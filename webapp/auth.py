"""Invitation-only accounts, hashed passwords and revocable server sessions."""
import hashlib
import hmac
import json
from http.cookies import SimpleCookie, CookieError
import os
import re
import secrets
import time


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return salt + ':' + value.hex()


class Accounts:
    def __init__(self, manager, local_setup=False):
        self.manager, self.db, self.lock = manager, manager.db, manager.lock
        self.local_setup = local_setup
        self.secure = os.environ.get('ROADTOASSETTO_SECURE_COOKIE', '0') == '1'
        self.attempts = {}
        with self.lock:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
                    password TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invites (token TEXT PRIMARY KEY, creator TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS route_owners (route_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY(route_id,user_id));
            ''')
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
            if 'owner_id' not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN owner_id TEXT')
            self.db.commit()
        self.bootstrap = manager.root / 'bootstrap-code.txt'
        if not self.has_users() and not local_setup and not self.bootstrap.exists():
            fd = os.open(self.bootstrap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(24))

    def has_users(self):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM users LIMIT 1').fetchone())

    def throttle(self, address):
        now = time.time()
        with self.lock:
            self.attempts = {key: value for key, value in self.attempts.items() if value[-1] > now-900}
            values = [v for v in self.attempts.get(address, []) if v > now-900]
            if len(values) >= 15:
                raise AuthError('Zu viele Anmeldeversuche. Bitte in 15 Minuten erneut versuchen.', 429)
            self.attempts[address] = values + [now]

    def register(self, data, address):
        self.throttle(address)
        username, password = data.get('username', ''), data.get('password', '')
        code = data.get('invite', '')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}', username):
            raise AuthError('Benutzername: 3–32 Zeichen, Buchstaben, Zahlen, Punkt, Minus oder Unterstrich.')
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise AuthError('Bitte ein Passwort mit 12 bis 256 Zeichen wählen.')
        if not isinstance(code, str) or len(code) > 128:
            raise AuthError('Ungültiger Einladungscode.')
        hashed = password_hash(password)
        with self.lock:
            first = not self.has_users()
            invitation = self.db.execute('SELECT * FROM invites WHERE token=?', (digest(code),)).fetchone()
            if first:
                if not self.local_setup and (not self.bootstrap.is_file() or not hmac.compare_digest(code, self.bootstrap.read_text().strip())):
                    raise AuthError('Für das erste Konto ist der Einrichtungscode aus /data/bootstrap-code.txt erforderlich.', 403)
            elif not invitation or invitation['used'] or invitation['expires'] <= time.time():
                raise AuthError('Der Einladungscode ist ungültig, abgelaufen oder bereits verwendet.', 403)
            if self.db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise AuthError('Dieser Benutzername ist bereits vergeben.', 409)
            user_id = secrets.token_hex(16)
            self.db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user_id, username, hashed, int(first), time.time()))
            if first:
                # Preserve legacy exports privately for the first operator account.
                self.db.execute('UPDATE jobs SET owner_id=? WHERE owner_id IS NULL', (user_id,))
                for row in self.db.execute('SELECT request FROM jobs WHERE owner_id=?',(user_id,)):
                    route_id=json.loads(row['request']).get('route_id')
                    if isinstance(route_id,str) and re.fullmatch('[a-f0-9]{32}',route_id):
                        self.db.execute('INSERT OR IGNORE INTO route_owners VALUES (?,?)',(route_id,user_id))
            else:
                self.db.execute('UPDATE invites SET used=1 WHERE token=?', (digest(code),))
            self.db.commit()
            if first and self.bootstrap.exists():
                self.bootstrap.unlink()
            return self.new_session(user_id)

    def login(self, data, address):
        self.throttle(address)
        username, password = data.get('username', ''), data.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str) or len(username)>32 or len(password)>256:
            raise AuthError('Benutzername oder Passwort ist falsch.', 401)
        with self.lock:
            row = self.db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        stored = row['password'] if row else '0'*32 + ':' + '0'*64
        if not hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored) or not row:
            raise AuthError('Benutzername oder Passwort ist falsch.', 401)
        return self.new_session(row['id'])

    def new_session(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            self.db.execute('INSERT INTO sessions VALUES (?,?,?)', (digest(token), user_id, time.time()+7*86400))
            self.db.commit()
        return token

    def read(self, headers):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get('Cookie', ''))
            token = cookie['rtac_session'].value if 'rtac_session' in cookie else ''
        except CookieError:
            token = ''
        if not 20 <= len(token) <= 128:
            return None
        with self.lock:
            row = self.db.execute('SELECT u.id,u.username,u.admin FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (digest(token), time.time())).fetchone()
        return {**dict(row), 'csrf': hmac.new(token.encode(), b'rtac-csrf-v1', hashlib.sha256).hexdigest(), '_token': digest(token)} if row else None

    def public(self, user):
        return {k: user[k] for k in ('id', 'username', 'admin', 'csrf')} if user else None

    def cookie(self, token=''):
        return f'rtac_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={604800 if token else 0}' + ('; Secure' if self.secure else '')

    def logout(self, user):
        with self.lock:
            self.db.execute('DELETE FROM sessions WHERE token=?', (user['_token'],))
            self.db.commit()

    def invite(self, user):
        if not user['admin']:
            raise AuthError('Nur der Administrator kann Einladungen erstellen.', 403)
        token = secrets.token_urlsafe(24)
        expires = time.time()+7*86400
        with self.lock:
            self.db.execute('INSERT INTO invites VALUES (?,?,?,0)', (digest(token), user['id'], expires))
            self.db.commit()
        return {'code': token, 'expires_at': expires, 'uses': 1}

    def own_route(self, user, route_id, grant=False):
        if not isinstance(route_id,str) or not re.fullmatch('[a-f0-9]{32}',route_id):
            raise AuthError('Ungültige Streckenkennung.',400)
        with self.lock:
            if grant:
                self.db.execute('INSERT OR IGNORE INTO route_owners VALUES (?,?)', (route_id, user['id']))
                self.db.commit()
            return bool(self.db.execute('SELECT 1 FROM route_owners WHERE route_id=? AND user_id=?', (route_id, user['id'])).fetchone())

    def owns_job(self, user, job_id):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM jobs WHERE id=? AND owner_id=?', (job_id, user['id'])).fetchone())
