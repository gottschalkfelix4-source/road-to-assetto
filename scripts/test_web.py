"""HTTP behavior, bounded jobs and artifact isolation against a real local server."""
from pathlib import Path
from html.parser import HTMLParser
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urljoin,urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from webapp.server import create_server
from webapp.catalog import template,validate_request
from webapp.jobs import JobManager,terminate_builder
from unittest.mock import patch


class LocalAssets(HTMLParser):
    """Read the actual document references, so path regressions cannot hide."""
    def __init__(self):
        super().__init__()
        self.assets=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='link' and 'stylesheet' in attrs.get('rel','').split() and attrs.get('href'):
            self.assets.append(('css',attrs['href']))
        if tag=='script' and attrs.get('src'):
            self.assets.append(('js',attrs['src']))


class OwnedChildProbe:
    """Observe/clean up only the child PID created by this test's worker.

    Windows keeps an open process handle, avoiding PID-reuse ambiguity and
    allowing a real exit check rather than inferring exit from a quiet log.
    """
    def __init__(self,pid):
        self.pid=pid
        self.handle=None
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            self.kernel.OpenProcess.restype=wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype=wintypes.DWORD
            self.kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
            self.kernel.TerminateProcess.restype=wintypes.BOOL
            self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            self.kernel.CloseHandle.restype=wintypes.BOOL
            self.handle=self.kernel.OpenProcess(0x00100000|0x0001,False,pid) # SYNCHRONIZE | TERMINATE
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

    def running(self):
        if os.name=='nt':
            result=self.kernel.WaitForSingleObject(self.handle,0)
            if result not in (0,258):
                raise RuntimeError('Could not inspect the test child process')
            return result==258 # WAIT_TIMEOUT; WAIT_OBJECT_0 means exited
        try:
            os.kill(self.pid,0)
        except ProcessLookupError:
            return False
        # A terminated orphan can briefly remain a zombie on Unix; it is no
        # longer running and may await adoption/reaping by the init process.
        stat=Path('/proc')/str(self.pid)/'stat'
        try:
            if stat.exists() and stat.read_text().rsplit(')',1)[1].split()[0]=='Z':
                return False
        except FileNotFoundError:
            return False
        if sys.platform=='darwin':
            state=subprocess.run(['ps','-p',str(self.pid),'-o','stat='],capture_output=True,text=True,check=False).stdout.strip()
            return bool(state) and not state.startswith('Z')
        return True

    def close(self):
        try:
            if self.running():
                if os.name=='nt':
                    self.kernel.TerminateProcess(self.handle,1)
                    self.kernel.WaitForSingleObject(self.handle,5000)
                else:
                    try: os.kill(self.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
        finally:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle=None

class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='web-test-')
        cls.server=create_server(port=0,data_root=cls.temp.name,start_worker=False)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.thread.join();cls.server.server_close();cls.server.manager.close();cls.temp.cleanup()

    def call(self,path,body=None,headers=None):
        h={'Content-Type':'application/json'};h.update(headers or {})
        req=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        try:
            with urlopen(req,timeout=10) as response:
                return response.status,response.headers,response.read()
        except HTTPError as error:
            return error.code,error.headers,error.read()

    def test_catalog_contains_real_map_and_route(self):
        status,_,raw=self.call('/api/catalog');self.assertEqual(status,200)
        t=json.loads(raw)['templates'][0]
        self.assertGreater(len(t['map']['route']),500)
        self.assertAlmostEqual(t['map']['route'][0][2],0)
        status,headers,data=self.call(t['map']['image_url'])
        self.assertEqual(status,200);self.assertTrue(data.startswith(b'\xff\xd8'))

    def test_document_stylesheets_and_scripts_are_served_with_correct_types(self):
        status,headers,raw=self.call('/')
        self.assertEqual(status,200)
        self.assertEqual(headers.get_content_type(),'text/html')
        document=LocalAssets();document.feed(raw.decode('utf-8'))
        self.assertIn('css',{kind for kind,_ in document.assets})
        self.assertIn('js',{kind for kind,_ in document.assets})
        for kind,reference in document.assets:
            address=urlsplit(urljoin(self.url+'/',reference))
            with self.subTest(asset=reference):
                self.assertEqual(address.netloc,urlsplit(self.url).netloc,'UI assets must remain local')
                self.assertEqual(address.scheme,'http')
                status,headers,body=self.call(address.path+('?' + address.query if address.query else ''))
                self.assertEqual(status,200)
                expected={'text/css'} if kind=='css' else {'text/javascript','application/javascript'}
                self.assertIn(headers.get_content_type(),expected)
                self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
                self.assertGreater(len(body),0)

    def test_invalid_inputs_do_not_queue(self):
        for body in ({'template_id':'elsewhere'},{'template_id':'altenhain','start_m':120,'end_m':130},
                     {'template_id':'altenhain','road_width_m':20},{'template_id':'altenhain','vegetation_density':float('nan')},
                     {'template_id':'altenhain','data_dir':'../../outside'}):
            self.assertEqual(self.call('/api/jobs',body)[0],400)

    def test_cross_site_writes_and_host_rebinding_rejected(self):
        self.assertEqual(self.call('/api/jobs',{'template_id':'altenhain'},{'Origin':'https://unrelated.example'})[0],403)
        self.assertEqual(self.call('/api/health',headers={'Host':'unrelated.example'})[0],403)

    def test_no_arbitrary_file_access(self):
        for path in ('/../README.md','/api/jobs/../../README.md/download','/.git/config','/web-data/jobs.sqlite3'):
            self.assertEqual(self.call(path)[0],404)

    def test_queue_persists_and_is_bounded(self):
        created=[]
        for i in range(5):
            status,_,data=self.call('/api/jobs',{'template_id':'altenhain','name':'Test <'+str(i)+'>','start_m':0,'end_m':200})
            self.assertEqual(status,202);created.append(json.loads(data))
        self.assertEqual(len({j['id'] for j in created}),5)
        self.assertEqual(self.call('/api/jobs',{'template_id':'altenhain'})[0],429)
        first=created[0]
        self.assertEqual(self.call('/api/jobs/'+first['id']+'/download')[0],404)
        self.assertEqual(json.loads(self.call('/api/jobs/'+first['id'])[2])['name'],'Test <0>')

    def test_large_legacy_history_keeps_geometry_out_of_poll_responses(self):
        job=json.loads(self.call('/api/jobs',{'template_id':'altenhain','name':'Legacy export'})[2])
        summary={'length_m':150,'buildings':4,'preview_kind':'render','preview_views':['overview','driver'],
                 'building_base_records':[{'vertices':[1,2,3]*1000}]*100,
                 'source_route':{'id':'a'*32,'warnings':['DOM1 unavailable'],'context_key':'private-cache-detail'}}
        self.server.manager._update(job['id'],status='succeeded',summary=json.dumps(summary))
        raw=self.call('/api/jobs')[2]
        self.assertLess(len(raw),5000)
        public=json.loads(raw)['jobs'][0]
        self.assertEqual(public['summary']['buildings'],4)
        self.assertEqual(public['summary']['source_route']['warnings'],['DOM1 unavailable'])
        self.assertNotIn('building_base_records',public['summary'])
        self.assertIn('driver',public['preview_urls'])
        stored=self.server.manager.db.execute('SELECT summary FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]
        self.assertIn('building_base_records',json.loads(stored))

    def test_worker_ownership_lock(self):
        with self.assertRaises(RuntimeError):
            JobManager(self.temp.name,start_worker=False)

    def test_restart_preserves_history_and_marks_interrupted_build(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='restart-test-') as temp:
            first=JobManager(temp,start_worker=False)
            queued=first.create({'template_id':'altenhain','name':'Weiter bauen'})
            interrupted=first.create({'template_id':'altenhain','name':'Unterbrochener Bau'})
            first._update(interrupted['id'],status='running')
            first.close()
            second=JobManager(temp,start_worker=False)
            try:
                self.assertEqual(second.get(queued['id'])['status'],'queued')
                self.assertEqual(second.get(interrupted['id'])['status'],'failed')
                self.assertIsNone(second.get(interrupted['id'])['download_url'])
            finally:
                second.close()

    def test_request_rejects_nonfinite_ranges_and_unsupported_types(self):
        for field,value in [('start_m',True),('end_m',float('inf')),('scenery_quality',[]),('name',[])]:
            with self.assertRaises(ValueError):
                validate_request({'template_id':'altenhain',field:value})

    def test_terminate_builder_stops_own_child_and_preserves_unrelated_process(self):
        # These are disposable Python sleepers, never an existing game/Blender
        # process. The sibling deliberately uses the same executable as worker
        # and child, so an executable-name-wide termination would be incorrect.
        worker_code='''
import json,os,subprocess,sys
from pathlib import Path
flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],**flags)
ready=Path(sys.argv[1])
temporary=ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'child_pid':child.pid}))
temporary.replace(ready)
try:
    child.wait()
finally:
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=5)
'''
        flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='process-tree-test-') as temp:
            self.assertTrue(Path(temp).resolve().is_relative_to((ROOT/'test-results').resolve()))
            ready=Path(temp)/'ready.json'
            sibling=None;worker=None;child=None
            try:
                sibling=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],
                                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**flags)
                worker=subprocess.Popen([sys.executable,'-c',worker_code,str(ready)],
                                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**flags)
                deadline=time.monotonic()+10
                while not ready.exists() and worker.poll() is None and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertTrue(ready.is_file(),'Test worker did not start its child')
                child=OwnedChildProbe(json.loads(ready.read_text())['child_pid'])
                self.assertTrue(child.running(),'Test child exited before termination')
                self.assertIsNone(sibling.poll())
                terminate_builder(worker)
                self.assertIsNotNone(worker.poll(),'Worker remained active')
                deadline=time.monotonic()+5
                while child.running() and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertFalse(child.running(),'Worker exited but its renderer-like child remained active')
                self.assertIsNone(sibling.poll(),'Unrelated sibling process was terminated')
            finally:
                if child:
                    child.close()
                for process in (worker,sibling):
                    if process and process.poll() is None:
                        process.kill()
                    if process:
                        process.wait(timeout=5)

class AerialProjectionTests(unittest.TestCase):
    def test_projected_pixels_match_world_markers_and_cache_stays_in_appdata(self):
        import numpy as np
        from PIL import Image,ImageDraw
        from pyproj import Transformer
        from webapp import catalog
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='aerial-test-') as temporary:
            root=Path(temporary);data=root/'source';(data/'textures').mkdir(parents=True)
            (root/'tracks').mkdir()
            (root/'tracks/altenhain.json').write_text(json.dumps({'data_dir':'source'}))
            bounds=[461547,5556104,462466,5556755]
            (data/'bounds.json').write_text(json.dumps(bounds))
            source=Image.new('RGB',(1000,700),(15,25,35));draw=ImageDraw.Draw(source)
            markers=[(70,90),(930,600),(900,100)]
            for x,y in markers:draw.ellipse((x-3,y-3,x+3,y+3),fill=(250,15,15))
            original=data/'textures/ortho.jpg';source.save(original,quality=100,subsampling=0)
            original_bytes=original.read_bytes()
            with patch.object(catalog,'ROOT',root):
                output=catalog.projected_map(root/'state')
                layout=catalog._map_layout(str(data),catalog._map_stamp(data))
                stamp=output.stat().st_mtime_ns
                self.assertEqual(catalog.projected_map(root/'state'),output)
                self.assertEqual(output.stat().st_mtime_ns,stamp)
            self.assertTrue(output.is_relative_to(root/'state'))
            self.assertEqual(original.read_bytes(),original_bytes)
            image=np.asarray(Image.open(output))
            mercator=Transformer.from_crs(25832,3857,always_xy=True)
            project_bounds=Transformer.from_crs(4326,3857,always_xy=True)
            west,south,east,north=layout['geographic_bounds']
            left,bottom=project_bounds.transform(west,south);right,top=project_bounds.transform(east,north)
            for x,y in markers:
                utm_x=bounds[0]+(x+.5)/1000*(bounds[2]-bounds[0])
                utm_y=bounds[3]-(y+.5)/700*(bounds[3]-bounds[1])
                world_x,world_y=mercator.transform(utm_x,utm_y)
                column=round((world_x-left)/(right-left)*image.shape[1]-.5)
                row=round((top-world_y)/(top-bottom)*image.shape[0]-.5)
                pixel=image[row,column].astype(int)
                self.assertGreater(pixel[0]-pixel[1],100,'A world marker moved away from its projected image pixel')


if __name__=='__main__':unittest.main(verbosity=2)
