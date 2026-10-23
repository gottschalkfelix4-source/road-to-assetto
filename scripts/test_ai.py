"""Real local mock HTTP lifecycle; no live provider or user credentials used."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from webapp import ai


def image_data(size=(512,512)):
    image=Image.new('RGB',size,(190,185,174))
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,30,size[1]),fill=(105,99,92))
    draw.rectangle((0,0,size[0],18),fill=(139,135,125))
    stream=io.BytesIO();image.save(stream,format='PNG')
    return stream.getvalue()


class MockAPI(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def answer(self,value,status=200,headers=None):
        raw=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.end_headers()
        try:self.wfile.write(raw)
        except OSError:pass

    def do_GET(self):
        state=self.server.state
        state['gets'].append((self.path,self.headers.get('Authorization')))
        if self.path=='/image.png':
            self.answer(state['image'])
        elif self.path=='/foreign-secret':
            self.answer({'unexpected':True})
        elif self.path=='/v1/models':
            if state['mode']=='models-missing':self.answer({},404)
            elif state['mode']=='redirect':self.answer({},302,{'Location':'/foreign-secret'})
            elif state['mode']=='error':self.answer({'error':state['key']},401)
            else:self.answer({'data':[{'id':'gpt-image-custom'},{'id':'local-image'}, {'id':state['key']}, {'id':'bad\nname'}]})
        else:self.answer({},404)

    def do_POST(self):
        state=self.server.state
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        state['posts'].append((self.path,body,self.headers.get('Authorization')))
        mode=state['mode']
        if mode=='error' or mode=='partial-error' and len(state['posts'])==2:
            self.answer({'error':{'message':state['key']}},401);return
        if mode=='redirect':
            self.answer({},307,{'Location':'/foreign-secret'});return
        if mode=='large':
            self.send_response(200);self.send_header('Content-Length',str(ai.MAX_JSON_BYTES+1));self.end_headers();return
        if mode=='invalid-json':self.answer(b'{bad');return
        if mode=='large-stream':
            self.answer({'data': 'x'*4096});return
        if mode=='slow-stream':
            self.send_response(200);self.send_header('Content-Length','30');self.end_headers()
            try:
                for _ in range(30):self.wfile.write(b' ');self.wfile.flush();time.sleep(.01)
            except OSError:pass
            return
        if mode=='invalid-image':item={'b64_json':base64.b64encode(b'not an image').decode()}
        elif mode=='invalid-base64':item={'b64_json':'!!!'}
        elif mode=='url':item={'url':self.server.base+'/image.png'}
        elif mode=='foreign-url':item={'url':'https://not-the-configured-provider.invalid/image.png'}
        elif mode=='credential-url':item={'url':'http://user:pass@127.0.0.1:3/image.png'}
        else:item={'b64_json':base64.b64encode(state['image']).decode()}
        self.answer({'data':[item],'usage':{'input_tokens':12,'output_tokens':34,'total_tokens':46,'provider_secret':state['key']}})


class AITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),MockAPI)
        cls.server.daemon_threads=True
        cls.server.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(2)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='ai-test-')
        self.root=Path(self.tmp.name);self.base=self.server.base+'/v1'
        self.server.state={'mode':'base64','gets':[],'posts':[],'key':'local-test-token-DO-NOT-USE','image':image_data()}

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self,key=True,model='local-image'):
        payload={'provider':'compatible','base_url':self.base,'image_model':model}
        if key:payload['api_key']=self.server.state['key']
        return ai.save_settings(self.root,payload)

    def test_private_settings_lifecycle_and_destination_change(self):
        self.assertEqual(ai.public_settings(self.root)['image_model'],'')
        public=self.configure()
        self.assertTrue(public['api_key_configured'])
        self.assertNotIn('api_key',public)
        self.assertNotIn(self.server.state['key'],json.dumps(public))
        self.assertTrue(ai.save_settings(self.root,{'api_key':''})['api_key_configured'])
        self.assertTrue(ai.save_settings(self.root,{'base_url':self.base+'/'})['api_key_configured'])
        if os.name!='nt':self.assertEqual(stat.S_IMODE((self.root/'ai-settings.json').stat().st_mode),0o600)
        self.assertFalse(ai.save_settings(self.root,{'base_url':self.base+'/other'})['api_key_configured'])
        self.configure()
        self.assertFalse(ai.save_settings(self.root,{'provider':'openai'})['api_key_configured'])
        self.configure()
        self.assertFalse(ai.save_settings(self.root,{'clear_api_key':True})['api_key_configured'])

    def test_settings_validation_has_no_network_side_effect(self):
        self.configure()
        for payload in ({'clear_api_key':'yes'},{'provider':'unknown'},{'base_url':'https://user:pass@example.com/v1'},
                        {'base_url':'https://example.com/v1?api_key=secret'},{'base_url':'http://example.com/v1'},
                        {'image_model':'model\nheader'},{'api_key':'new','clear_api_key':True},
                        {'image_model':self.server.state['key']},{'api_key':'not-ascii-ä'}):
            with self.subTest(payload=list(payload)),self.assertRaises(ai.AIError):ai.save_settings(self.root,payload)
        self.assertEqual(self.server.state['posts'],[])
        self.assertEqual(self.server.state['gets'],[])

    def test_model_connection_is_not_an_image_test_and_redacts(self):
        self.configure()
        result=ai.test_connection(self.root)
        self.assertTrue(result['ok']);self.assertFalse(result['image_test'])
        self.assertEqual(result['models'],['gpt-image-custom','local-image'])
        self.assertEqual(self.server.state['posts'],[])
        self.server.state['mode']='models-missing'
        self.assertFalse(ai.test_connection(self.root)['ok'])

    def test_docker_host_alias_is_allowed_without_allowing_other_http_hosts(self):
        with patch.object(ai, '_request', side_effect=AssertionError('No network request expected')) as request:
            result=ai.save_settings(self.root,{'provider':'compatible',
                'base_url':'http://HOST.DOCKER.INTERNAL:8000/v1/','image_model':'local-image'})
            self.assertEqual(result['base_url'],'http://host.docker.internal:8000/v1')
            self.assertFalse(result['api_key_configured'])
            for host in ('host.docker.internal.example.com','host-docker.internal',
                         'api.example.com','image-server'):
                with self.subTest(host=host),self.assertRaises(ai.AIError):
                    ai.save_settings(self.root,{'base_url':'http://'+host+':8000/v1'})
            self.assertEqual(ai.public_settings(self.root)['base_url'],result['base_url'])
            request.assert_not_called()

    def test_fresh_generation_cached_repeat_and_model_identity(self):
        self.configure();progress=[]
        output=self.root/'pack'
        result=ai.generate_material_pack(self.root,output,lambda *args:progress.append(args))
        self.assertEqual(result['usage']['image_generation_calls'],3)
        self.assertFalse(result['cache_hit']);self.assertTrue(result['provenance']['synthetic'])
        self.assertFalse(result['provenance']['real_building_photographs'])
        self.assertNotIn(self.server.state['key'],json.dumps(result))
        self.assertEqual(progress[-1][1],100)
        for path in result['files'].values():
            with Image.open(path) as image:
                self.assertEqual(image.size,(1024,1024))
                self.assertEqual(image.crop((0,0,1,1024)).tobytes(),image.crop((1023,0,1024,1024)).tobytes())
                self.assertEqual(image.crop((0,0,1024,1)).tobytes(),image.crop((0,1023,1024,1024)).tobytes())
        for path,payload,header in self.server.state['posts']:
            self.assertEqual(path,'/v1/images/generations')
            self.assertEqual((payload['size'],payload['n'],payload['response_format']),('1024x1024',1,'b64_json'))
            self.assertNotIn('Altenhain',payload['prompt']);self.assertNotIn('Langstra',payload['prompt'])
        self.assertIn('1.25 metre',self.server.state['posts'][2][1]['prompt'])
        self.assertIn('6 metres wide',self.server.state['posts'][0][1]['prompt'])
        self.assertIn('approximately 3 metres high',self.server.state['posts'][0][1]['prompt'])
        self.assertEqual(result['provenance']['prompt_version'],'generic-albedo-v3-edge24')
        cached=ai.generate_material_pack(self.root,self.root/'cached')
        self.assertTrue(cached['cache_hit']);self.assertEqual(cached['usage']['image_generation_calls'],0)
        self.assertEqual(len(self.server.state['posts']),3)
        ai.save_settings(self.root,{'image_model':'other-image'})
        other=ai.generate_material_pack(self.root,self.root/'other')
        self.assertNotEqual(other['cache_key'],result['cache_key']);self.assertEqual(len(self.server.state['posts']),6)

    def test_openai_gpt_payload_has_no_unsupported_response_format(self):
        with patch.object(ai,'DEFAULT_URL',self.base):
            ai.save_settings(self.root,{'provider':'openai','base_url':self.base,'image_model':'gpt-image-custom','api_key':self.server.state['key']})
            ai.generate_material_pack(self.root,self.root/'openai')
        self.assertEqual(len(self.server.state['posts']),3)
        for _,payload,_ in self.server.state['posts']:
            self.assertNotIn('response_format',payload);self.assertEqual(payload['quality'],'high')

    def test_same_origin_url_download_never_receives_credentials(self):
        self.configure();self.server.state['mode']='url'
        result=ai.generate_material_pack(self.root,self.root/'urls')
        self.assertEqual(result['usage']['image_generation_calls'],3)
        self.assertEqual(self.server.state['gets'],[('/image.png',None)]*3)
        self.assertTrue(all(header is not None for _,_,header in self.server.state['posts']))

    def test_empty_key_works_for_local_compatible_service(self):
        self.configure(key=False)
        ai.generate_material_pack(self.root,self.root/'no-key')
        self.assertTrue(all(header is None for _,_,header in self.server.state['posts']))

    def test_unsafe_responses_fail_cleanly_without_redirect_or_download(self):
        for mode in ('redirect','foreign-url','credential-url','error','invalid-json','invalid-image','invalid-base64','large'):
            with self.subTest(mode=mode):
                self.configure(model='model-'+mode);self.server.state['mode']=mode
                with self.assertRaises(ai.AIError) as caught:ai.generate_material_pack(self.root,self.root/mode)
                self.assertNotIn(self.server.state['key'],str(caught.exception))
                self.assertNotIn('Authorization',str(caught.exception))
        self.assertEqual(self.server.state['gets'],[])

    def test_partial_failure_resumes_without_repaying_successful_image(self):
        self.configure();self.server.state['mode']='partial-error'
        with self.assertRaises(ai.AIError):ai.generate_material_pack(self.root,self.root/'partial')
        self.assertEqual(len(self.server.state['posts']),2)
        self.server.state['mode']='base64'
        result=ai.generate_material_pack(self.root,self.root/'retry')
        self.assertEqual(result['usage']['image_generation_calls'],2)
        self.assertEqual(result['usage']['cached_images'],1)
        self.assertEqual(len(self.server.state['posts']),4)

    def test_invalid_dimensions_and_timeout_are_bounded_safe_errors(self):
        for size in ((128,128),(512,256),(4097,4097)):
            with self.subTest(size=size),self.assertRaises(ai.AIError):ai._normalise_image(image_data(size))
        self.configure()
        with patch.object(ai.requests.Session,'request',side_effect=ai.requests.Timeout('secret '+self.server.state['key'])):
            with self.assertRaises(ai.AIError) as caught:ai.test_connection(self.root)
        self.assertEqual(caught.exception.code,'timeout')
        self.assertNotIn(self.server.state['key'],str(caught.exception))

    def test_concurrent_pack_lock_rejects_duplicate_generation(self):
        folder=self.root/'same-cache'
        with ai._cache_lock(folder):
            with self.assertRaises(ai.AIError) as caught:
                with ai._cache_lock(folder):pass
        self.assertEqual(caught.exception.code,'generation_busy')

    def test_stream_body_limit_and_slow_trickle_deadline(self):
        self.configure();self.server.state['mode']='large-stream'
        with self.assertRaises(ai.AIError) as caught:
            ai._request('POST',self.base+'/images/generations',payload={},limit=1024)
        self.assertEqual(caught.exception.code,'response_too_large')
        self.server.state['mode']='slow-stream'
        with patch.object(ai,'TOTAL_TIMEOUT',.04):
            started=time.monotonic()
            with self.assertRaises(ai.AIError) as caught:
                ai._request('POST',self.base+'/images/generations',payload={})
            self.assertLess(time.monotonic()-started,1)
        self.assertEqual(caught.exception.code,'timeout')


if __name__=='__main__':unittest.main(verbosity=2)
