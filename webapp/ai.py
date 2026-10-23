"""Explicit, optional image generation; credentials stay in local server state.

Only fixed generic material prompts leave this module. No location, facade
photograph or project document is sent to the configured provider.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
import requests

DEFAULT_URL = 'https://api.openai.com/v1'
PROMPT_VERSION = 'generic-albedo-v3-edge24'
MAX_JSON_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_777_216
MAX_MODELS = 100
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 150
TOTAL_TIMEOUT = 180
_settings_lock = threading.RLock()

_COMMON = (
    'Create one photorealistic physically based game material ALBEDO texture. '
    'Square 1024 by 1024 pixels, seamless tile repeating on every edge. '
    'Flat orthographic surface scan, evenly lit diffuse colour, no perspective, '
    'no cast shadows, no directional highlights, no ambient occlusion baked in. '
    'No objects, buildings, windows, doors, labels, writing, logos, people, '
    'frames or borders. The whole image is one continuous material. '
    'Natural restrained colours and fine photographic detail, not stylized. '
)
PROMPTS = {
    'plaster': _COMMON + 'Warm off-white exterior lime plaster, clean maintained render, '
        'very fine mineral grain and subtle weathering. Represent a surface 6 metres wide '
        'and approximately 3 metres high across this square image. The fine grain must '
        'have natural proportions when mapped onto that rectangular surface. '
        'No broad water stains, cracks, peeling patches or repeated decorative shapes.',
    'roof': _COMMON + 'Traditional muted reddish brown interlocking clay roof tiles, '
        'parallel horizontal overlapping rows, subtle individual clay colour variation '
        'and a little age. A 2 metre square surface with realistic 25-35 cm tile scale.',
    'asphalt': _COMMON + 'Dry medium-dark grey residential road asphalt, dense fine '
        'mineral aggregate with tiny stone flecks, subtly aged but maintained. '
        'A 1.25 metre square surface, no road markings, large cracks, patches, debris or puddles.',
}


class AIError(ValueError):
    """Safe public error. Never use an upstream response or exception as message."""
    def __init__(self, message, code='ai_error', status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _text(value, field, maximum, empty=True):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AIError(f'Ungültiges Feld: {field}.', 'invalid_settings')
    value = value.strip()
    if not empty and not value:
        raise AIError(f'Bitte {field} eintragen.', 'invalid_settings')
    return value


def _normal_url(value):
    value = _text(value, 'API-Adresse', 2048, False)
    if '\\' in value:
        raise AIError('Ungültige API-Adresse.', 'invalid_settings')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AIError('Ungültige API-Adresse.', 'invalid_settings') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AIError('Die API-Adresse muss HTTP(S) ohne Zugangsdaten, Abfrage oder Fragment sein.', 'invalid_settings')
    if parsed.scheme == 'http':
        host = parsed.hostname.lower()
        private = host in ('localhost', 'host.docker.internal')
        try:
            address = ipaddress.ip_address(host)
            private = address.is_loopback or any(address in network for network in
                (ipaddress.ip_network('10.0.0.0/8'), ipaddress.ip_network('172.16.0.0/12'),
                 ipaddress.ip_network('192.168.0.0/16'), ipaddress.ip_network('fc00::/7'))
                if address.version == network.version)
        except ValueError:
            pass
        if not private:
            raise AIError('Externe APIs benötigen HTTPS; HTTP ist für lokale Server erlaubt.', 'invalid_settings')
    host = parsed.hostname.lower()
    authority = '[' + host + ']' if ':' in host else host
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        authority += ':' + str(port)
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))


def _defaults():
    return {'provider': 'openai', 'base_url': DEFAULT_URL, 'image_model': '', 'api_key': ''}


def _load(data_root):
    path = Path(data_root) / 'ai-settings.json'
    if not path.exists():
        return _defaults()
    try:
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError()
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or set(result) != set(_defaults()):
            raise ValueError()
        result['base_url'] = _normal_url(result['base_url'])
        result['image_model'] = _text(result['image_model'], 'Bildmodell', 200)
        result['api_key'] = _text(result['api_key'], 'API-Schlüssel', 8192)
        if not result['api_key'].isascii():
            raise ValueError()
        if result['provider'] not in ('openai', 'compatible') or (result['provider'] == 'openai' and result['base_url'] != DEFAULT_URL):
            raise ValueError()
        return result
    except (OSError, ValueError, TypeError, KeyError):
        raise AIError('Die lokalen KI-Einstellungen konnten nicht gelesen werden.', 'settings_unreadable', 500) from None


def _public(settings):
    return {**{k: settings[k] for k in ('provider', 'base_url', 'image_model')},
            'api_key_configured': bool(settings['api_key'])}


def public_settings(data_root):
    with _settings_lock:
        return _public(_load(data_root))


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AIError('Unsicheres lokales Speicherziel.', 'storage_error', 500)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            if os.name != 'nt':
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path, value):
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))


def save_settings(data_root, payload):
    allowed = {'provider', 'base_url', 'image_model', 'api_key', 'clear_api_key'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise AIError('Ungültige KI-Einstellungen.', 'invalid_settings')
    if 'clear_api_key' in payload and not isinstance(payload['clear_api_key'], bool):
        raise AIError('clear_api_key muss wahr oder falsch sein.', 'invalid_settings')
    with _settings_lock:
        previous = _load(data_root)
        updated = dict(previous)
        provider = payload.get('provider', previous['provider'])
        if provider not in ('openai', 'compatible'):
            raise AIError('Unbekannter KI-Anbieter.', 'invalid_settings')
        updated['provider'] = provider
        updated['base_url'] = _normal_url(payload.get('base_url', DEFAULT_URL if provider == 'openai' else previous['base_url']))
        if provider == 'openai' and updated['base_url'] != DEFAULT_URL:
            raise AIError('Für eigene API-Adressen bitte OpenAI-kompatibel auswählen.', 'invalid_settings')
        updated['image_model'] = _text(payload.get('image_model', previous['image_model']), 'Bildmodell', 200)
        key = _text(payload.get('api_key', ''), 'API-Schlüssel', 8192)
        if not key.isascii():
            raise AIError('Der API-Schlüssel enthält ungültige Zeichen.', 'invalid_settings')
        changed_destination = any(updated[k] != previous[k] for k in ('provider', 'base_url'))
        if payload.get('clear_api_key') and key:
            raise AIError('Schlüssel löschen oder ersetzen, nicht gleichzeitig.', 'invalid_settings')
        if changed_destination or payload.get('clear_api_key'):
            updated['api_key'] = ''
        if key:
            updated['api_key'] = key
        if updated['api_key'] and any(updated['api_key'] in updated[field] for field in ('base_url','image_model')):
            raise AIError('API-Schlüssel bitte ausschließlich im Schlüsselfeld speichern.', 'invalid_settings')
        try:
            _json_write(Path(data_root) / 'ai-settings.json', updated)
        except OSError:
            raise AIError('Die KI-Einstellungen konnten nicht gespeichert werden.', 'storage_error', 500) from None
        return _public(updated)


def _ready(settings, require_model=True):
    if require_model and not settings['image_model']:
        raise AIError('Bitte zuerst ein Bildmodell in den KI-Einstellungen eintragen.', 'model_missing')
    if settings['provider'] == 'openai' and not settings['api_key']:
        raise AIError('Bitte zuerst einen OpenAI-API-Schlüssel in den KI-Einstellungen speichern.', 'key_missing')


def _request(method, url, *, key='', payload=None, limit=MAX_JSON_BYTES, timeout=READ_TIMEOUT):
    # A fresh session prevents cookies/netrc credentials being forwarded to an
    # image download. Environment proxies/auth are intentionally not inherited.
    try:
        with requests.Session() as session:
            session.trust_env = False
            headers = {'Accept': 'application/json' if payload is not None or method == 'GET' else '*/*',
                       'User-Agent': 'RoadToAssetto/0.5'}
            if key:
                headers['Authorization'] = 'Bearer ' + key
            started = time.monotonic()
            with session.request(method, url, headers=headers, json=payload, stream=True,
                                 allow_redirects=False, timeout=(CONNECT_TIMEOUT, timeout)) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise AIError('Die API hat umgeleitet. Bitte die endgültige API-Adresse eintragen.', 'redirect_rejected', 502)
                if status >= 400:
                    message = {401:'Der API-Schlüssel wurde abgelehnt.', 403:'Der Anbieter erlaubt diese Anfrage nicht.',
                               404:'Der API-Endpunkt oder das Bildmodell ist nicht verfügbar.',
                               429:'Das Anbieterlimit wurde erreicht. Bitte später erneut versuchen.'}.get(status,
                               'Der KI-Anbieter konnte die Anfrage nicht ausführen.')
                    raise AIError(message + f' (HTTP {status})', 'upstream_http_' + str(status), 502)
                declared = response.headers.get('Content-Length')
                if declared and declared.isdecimal() and int(declared) > limit:
                    raise AIError('Die Anbieterantwort überschreitet die Größenbegrenzung.', 'response_too_large', 502)
                data = bytearray()
                # read1 returns after available network data instead of waiting
                # for a full 64 KiB chunk; a slow trickle cannot evade the
                # overall streaming deadline indefinitely.
                while True:
                    if time.monotonic() - started > TOTAL_TIMEOUT:
                        raise AIError('Die Anfrage hat zu lange gedauert.', 'timeout', 504)
                    try:
                        chunk = response.raw.read1(65536, decode_content=True)
                    except requests.packages.urllib3.exceptions.ReadTimeoutError:
                        raise AIError('Der KI-Anbieter hat nicht rechtzeitig geantwortet.', 'timeout', 504) from None
                    except (OSError, requests.packages.urllib3.exceptions.HTTPError):
                        raise AIError('Die Anbieterantwort wurde unterbrochen.', 'connection_error', 502) from None
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise AIError('Die Anbieterantwort überschreitet die Größenbegrenzung.', 'response_too_large', 502)
                return bytes(data)
    except requests.Timeout:
        raise AIError('Der KI-Anbieter hat nicht rechtzeitig geantwortet.', 'timeout', 504) from None
    except requests.RequestException:
        raise AIError('Der KI-Anbieter ist nicht erreichbar oder die Verbindung wurde unterbrochen.', 'connection_error', 502) from None


def _request_json(method, settings, endpoint, payload=None, limit=MAX_JSON_BYTES, timeout=READ_TIMEOUT):
    raw = _request(method, settings['base_url'] + endpoint, key=settings['api_key'], payload=payload, limit=limit, timeout=timeout)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise AIError('Der Anbieter hat keine gültige JSON-Antwort geliefert.', 'invalid_response', 502) from None


def test_connection(data_root):
    with _settings_lock:
        settings = _load(data_root)
    _ready(settings, require_model=False)
    try:
        data = _request_json('GET', settings, '/models', limit=1024*1024, timeout=20)
    except AIError as error:
        if error.code in ('upstream_http_404', 'upstream_http_405'):
            return {'ok': False, 'models': [], 'image_test': False,
                    'message': 'Dieser Anbieter bietet keine Modellliste unter /models an. Das Bildmodell kann trotzdem manuell eingetragen werden; Bilderzeugung wurde nicht getestet.'}
        raise
    if not isinstance(data.get('data'), list):
        raise AIError('Die Antwort enthält keine gültige Modellliste.', 'invalid_response', 502)
    models = set()
    for item in data['data']:
        name = item.get('id') if isinstance(item,dict) else None
        if (isinstance(name,str) and 0 < len(name) <= 200 and not any(ord(c)<32 for c in name)
                and not (settings['api_key'] and settings['api_key'] in name)):
            models.add(name)
    models = sorted(models)
    return {'ok': True, 'models': models[:MAX_MODELS], 'image_test': False,
            'message': 'Verbindung und Modellliste geprüft. Keine Bilder erzeugt; die Unterstützung des gewählten Bildmodells ist noch nicht geprüft.'}


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


def _image_bytes(settings, item):
    if not isinstance(item, dict):
        raise AIError('Der Anbieter hat kein Bild zurückgegeben.', 'image_missing', 502)
    encoded = item.get('b64_json')
    if isinstance(encoded, str) and encoded:
        if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
            raise AIError('Das erzeugte Bild ist zu groß.', 'image_too_large', 502)
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AIError('Das Bild ist nicht gültig Base64-kodiert.', 'invalid_image', 502) from None
    url = item.get('url')
    if not isinstance(url, str) or len(url) > 4096:
        raise AIError('Der Anbieter hat kein Bild zurückgegeben.', 'image_missing', 502)
    try:
        parsed = urlsplit(url)
        safe = parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password and not parsed.fragment
        safe = safe and _origin(url) == _origin(settings['base_url']) and '\\' not in url and not any(ord(c)<32 for c in url)
    except (ValueError, AttributeError):
        safe = False
    if not safe:
        raise AIError('Bild-URLs müssen dieselbe Origin wie die API verwenden. Bitte den Anbieter auf b64_json-Ausgabe einstellen.', 'image_url_rejected', 502)
    return _request('GET', url, limit=MAX_IMAGE_BYTES, timeout=30)


def _normalise_image(raw):
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise AIError('Das erzeugte Bild ist leer oder zu groß.', 'invalid_image', 502)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if image.format not in ('PNG', 'JPEG', 'WEBP') or min(width,height)<256 or width!=height or width*height>MAX_IMAGE_PIXELS:
                    raise ValueError()
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError()
                image.load()
                image = ImageOps.exif_transpose(image).convert('RGB').resize((1024,1024), Image.Resampling.LANCZOS)
                # The prompt alone cannot guarantee seamless image edges. A
                # narrow symmetric blend gives equal opposite border pixels
                # while preserving the generated material's centre detail.
                for horizontal in (True, False):
                    original = image.copy()
                    for edge in range(24):
                        a = (edge,0,edge+1,1024) if horizontal else (0,edge,1024,edge+1)
                        b = (1023-edge,0,1024-edge,1024) if horizontal else (0,1023-edge,1024,1024-edge)
                        first, last = original.crop(a), original.crop(b)
                        weight = .5*(1-edge/24)
                        image.paste(Image.blend(first,last,weight), a)
                        image.paste(Image.blend(last,first,weight), b)
                output = io.BytesIO()
                image.save(output, format='PNG')
                return output.getvalue()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AIError('Die Antwort enthält keine gültige quadratische PNG-, JPEG- oder WebP-Textur.', 'invalid_image', 502) from None


@contextmanager
def _cache_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AIError('Diese KI-Texturen werden bereits erzeugt. Bitte kurz warten.', 'generation_busy', 409) from None
        yield


def _numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    return {k: value[k] for k in ('input_tokens','output_tokens','total_tokens')
            if isinstance(value.get(k), int) and not isinstance(value[k], bool) and 0 <= value[k] <= 10**12}


def generate_material_pack(data_root, output_dir, progress=None):
    """Generate/reuse exactly three generic materials after an explicit action.

    Failed partial packs retain their successful images to avoid paying for
    them again. There are no automatic upstream retries or model fallbacks.
    """
    with _settings_lock:
        settings = _load(data_root)
    _ready(settings)
    identity = {k:settings[k] for k in ('provider','base_url','image_model')}
    identity.update(prompt_version=PROMPT_VERSION, prompts=PROMPTS, size='1024x1024')
    cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(data_root) / 'ai-cache' / cache_key
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    calls = 0;cached = 0;files = {};records = {};provider_usage = []
    with _cache_lock(cache):
        for index, (kind, prompt) in enumerate(PROMPTS.items()):
            if progress:
                progress(f'KI-Material {index+1}/3: {kind}', int(index/3*100))
            filename = 'ai_' + kind + '.png'
            path = cache / filename
            record_path = cache / (kind + '.json')
            record = None
            if path.is_file() and record_path.is_file():
                try:
                    candidate = json.loads(record_path.read_text(encoding='utf-8'))
                    if isinstance(candidate,dict) and path.stat().st_size <= MAX_IMAGE_BYTES and hashlib.sha256(path.read_bytes()).hexdigest() == candidate.get('sha256'):
                        _normalise_image(path.read_bytes())
                        record = candidate
                except (OSError, ValueError, TypeError):
                    pass
            if record is not None:
                cached += 1
            else:
                payload = {'model':settings['image_model'], 'prompt':prompt, 'n':1, 'size':'1024x1024'}
                if settings['provider'] == 'openai':
                    payload['quality'] = 'high'
                if settings['provider'] != 'openai' and not settings['image_model'].startswith(('gpt-image-', 'chatgpt-image-')):
                    payload['response_format'] = 'b64_json'
                data = _request_json('POST', settings, '/images/generations', payload)
                calls += 1
                if not isinstance(data.get('data'), list) or len(data['data']) != 1:
                    raise AIError('Der Anbieter muss genau ein Bild je Material zurückgeben.', 'invalid_response', 502)
                image = _normalise_image(_image_bytes(settings, data['data'][0]))
                usage = _numeric_usage(data.get('usage'))
                provider_usage.append(usage)
                record = {'kind':kind,'file':filename,'sha256':hashlib.sha256(image).hexdigest(),
                          'created_at':datetime.now(timezone.utc).isoformat(),'provider_usage':usage}
                _atomic_bytes(path, image)
                _json_write(record_path, record)
            _atomic_bytes(destination / filename, path.read_bytes())
            files[kind] = str(destination / filename)
            records[kind] = record
        provenance = {'type':'ai_generated_generic_materials', 'synthetic':True,
            'real_building_photographs':False, 'geographic_accuracy':False,
            'description':'Synthetische generische Materialtexturen; keine Originalfotos und keine Rekonstruktion örtlicher Gebäude.',
            'provider':settings['provider'],'base_url':settings['base_url'],'image_model':settings['image_model'],
            'prompt_version':PROMPT_VERSION,'cache_key':cache_key,'materials':records,
            'edge_blend_pixels':24,'output_size':[1024,1024],
            'rights_note':'Generated using the configured provider; usage rights follow that provider and model. Not labelled CC0.',
            'data_sent':'Only fixed generic plaster, roof-tile and asphalt descriptions; no location, photos or track data.'}
        _json_write(cache / 'provenance.json', provenance)
        _json_write(destination / 'ai_materials.json', provenance)
    if progress:
        progress('KI-Materialien bereit', 100)
    return {'cache_key':cache_key,'cache_hit':cached==len(PROMPTS),'files':files,'provenance':provenance,
            'usage':{'image_generation_calls':calls,'cached_images':cached,'images_generated':calls,'provider_usage':provider_usage}}
