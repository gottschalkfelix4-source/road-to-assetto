"""Real HTTP registration, session and private artifact authorization tests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import requests
from webapp.server import create_server


def authenticated_headers(server, username='test_operator'):
    token=server.accounts.register({'username':username,'password':'Disposable test password 2026!'},'fixture')
    user=server.accounts.read({'Cookie':'rtac_session='+token})
    return {'Cookie':'rtac_session='+token,'X-CSRF-Token':user['csrf']}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.server=create_server(port=0,data_root=self.tmp.name,start_worker=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.a=requests.Session();self.b=requests.Session()
        for s in (self.a,self.b):s.trust_env=False
    def tearDown(self):
        self.a.close();self.b.close();self.server.shutdown();self.thread.join();self.server.server_close();self.server.manager.close();self.tmp.cleanup()
    def post(self,session,path,**data):return session.post(self.url+path,json=data,timeout=10)
    def register(self,session,name,invite=''):
        response=self.post(session,'/api/auth/register',username=name,password='Disposable strong passphrase!',invite=invite)
        if response.status_code==201:session.headers['X-CSRF-Token']=response.json()['user']['csrf']
        return response
    def pair(self):
        self.assertEqual(self.register(self.a,'owner').status_code,201)
        invite=self.post(self.a,'/api/auth/invite').json()['code']
        self.assertEqual(self.register(self.b,'friend',invite).status_code,201)
        return invite
    def test_invitation_required_once_only_and_password_hashing(self):
        self.pair()
        self.assertEqual(self.register(self.b,'stranger').status_code,403)
        self.assertEqual(self.post(self.b,'/api/auth/invite').status_code,403)
        with self.server.manager.lock:
            values=self.server.manager.db.execute('SELECT password FROM users').fetchall()
        self.assertTrue(all('Disposable' not in r[0] and len(r[0])==97 for r in values))
    def test_private_jobs_artifacts_and_routes_even_from_admin(self):
        self.pair()
        created=self.post(self.b,'/api/jobs',template_id='altenhain',name='Private friend road').json()
        identifier=created['id']
        self.assertEqual(self.a.get(self.url+'/api/jobs').json()['jobs'],[])
        for suffix in ('','/download','/preview.png','/driver.png','/environment.png'):
            self.assertEqual(self.a.get(self.url+'/api/jobs/'+identifier+suffix).status_code,404)
        self.assertEqual(self.b.get(self.url+'/api/jobs/'+identifier).status_code,200)
        self.assertEqual(self.a.get(self.url+'/api/routes/'+'1'*32).status_code,404)
        response=self.post(self.a,'/api/jobs',template_id='hessen',route_id='1'*32)
        self.assertEqual(response.status_code,404)
    def test_session_csrf_logout_and_anonymous_assets(self):
        response=self.register(self.a,'operator')
        self.assertIn('HttpOnly',response.headers['Set-Cookie']);self.assertIn('SameSite=Lax',response.headers['Set-Cookie'])
        self.assertEqual(self.b.get(self.url+'/api/jobs').status_code,401)
        self.assertEqual(self.b.get(self.url+'/api/health').status_code,200)
        self.assertEqual(self.b.get(self.url+'/app.js').status_code,200)
        csrf=self.a.headers.pop('X-CSRF-Token')
        self.assertEqual(self.post(self.a,'/api/auth/invite').status_code,403)
        self.a.headers['X-CSRF-Token']=csrf
        self.a.headers['Origin']='https://unrelated.example'
        self.assertEqual(self.post(self.a,'/api/auth/invite').status_code,403)
        del self.a.headers['Origin']
        self.assertEqual(self.post(self.a,'/api/auth/logout').status_code,200)
        self.assertEqual(self.a.get(self.url+'/api/jobs').status_code,401)
    def test_invite_cannot_be_reused(self):
        invite=self.pair()
        self.assertEqual(self.register(self.b,'another',invite).status_code,403)
    def test_only_owner_can_resume_failed_build(self):
        self.pair()
        identifier=self.post(self.b,'/api/jobs',template_id='altenhain',name='Retry road').json()['id']
        self.server.manager._update(identifier,status='failed',error='Provider unavailable')
        self.assertEqual(self.post(self.a,'/api/jobs/'+identifier+'/retry').status_code,404)
        self.assertEqual(self.post(self.b,'/api/jobs/'+identifier+'/retry').status_code,202)
        self.assertEqual(self.post(self.b,'/api/jobs/'+identifier+'/retry').status_code,400)
    def test_old_jobs_are_claimed_once_and_remain_private(self):
        legacy=self.server.manager.create({'template_id':'altenhain','name':'Legacy'})
        self.pair()
        self.assertEqual(self.a.get(self.url+'/api/jobs/'+legacy['id']).status_code,200)
        self.assertEqual(self.b.get(self.url+'/api/jobs/'+legacy['id']).status_code,404)
    def test_login_wrong_password_and_session_reuse(self):
        self.register(self.a,'operator')
        self.assertEqual(self.post(self.b,'/api/auth/login',username='operator',password='wrong').status_code,401)
        r=self.post(self.b,'/api/auth/login',username='operator',password='Disposable strong passphrase!')
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.b.get(self.url+'/api/auth/session').json()['user']['username'],'operator')
    def test_network_bootstrap_requires_local_file_code(self):
        self.server.accounts.local_setup=False
        self.server.accounts.bootstrap.write_text('test-bootstrap-code')
        self.assertEqual(self.register(self.a,'operator').status_code,403)
        self.assertEqual(self.register(self.a,'operator','test-bootstrap-code').status_code,201)
        self.assertFalse(self.server.accounts.bootstrap.exists())


if __name__=='__main__':unittest.main()
