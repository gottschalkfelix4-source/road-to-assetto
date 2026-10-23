"""Bounded, private per-build timing; never records URLs, keys or payloads."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import threading
import time

PHASES = {'prepare':'Vorbereitung', 'data':'Geodaten', 'model':'Modellbau',
          'clip':'Abschnitt zuschneiden', 'export':'KN5 und Geometrieprüfung',
          'preview':'Vorschauen', 'package':'Streckenpaket', 'wait':'Wiederholungswartezeit', 'ai':'KI-Materialien'}
PROVIDERS = ('Hessen DGM1','Hessen DOM1','Hessen DOP20','Hessen LoD2','OSM Overpass','OSM Geofabrik','OSM (Cache)','Vorbereitete Altenhain-Daten','Geodatendienst')
_active = ContextVar('build_metrics', default=None)


def provider_name(url):
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    if parsed.hostname=='inspire-hessen.de':
        for part,name in (('/dgm1/','Hessen DGM1'),('/dom1/','Hessen DOM1'),('/dop20/','Hessen DOP20')):
            if part in parsed.path:return name
        return 'Hessen LoD2'
    if parsed.hostname=='download.geofabrik.de':return 'OSM Geofabrik'
    if parsed.path.endswith('/interpreter'):return 'OSM Overpass'
    return 'Geodatendienst'


def set_phase(key):
    monitor=_active.get()
    if monitor:monitor.phase(key)


def stage_phase(stage):
    text=stage.lower()
    if text=='fertig':return
    if 'ki-' in text:key='ai'
    elif any(s in text for s in ('paket','streckenpaket')):key='package'
    elif 'vorschau' in text:key='preview'
    elif any(s in text for s in ('exportieren','assetto-corsa-modell','kn5')):key='export'
    elif any(s in text for s in ('bauen','zwischenmodell')):key='model'
    elif any(s in text for s in ('laden','geodaten','quelldaten')):key='data'
    else:key='prepare'
    set_phase(key)


def cache_hit(provider):
    monitor=_active.get()
    if monitor:
        with monitor.lock:monitor.provider(provider)['cache_hits']+=1


@contextmanager
def retry_wait(seconds):
    monitor=_active.get();previous=monitor.current if monitor else None
    if monitor:
        with monitor.lock:monitor.retries+=1
        monitor.phase('wait')
    try:yield
    finally:
        if monitor:monitor.phase(previous)


class RequestTiming:
    def __init__(self,monitor,name):
        self.monitor=monitor;self.name=name;self.started=time.monotonic()
        self.header_seconds=None;self.status=None;self.bytes=0
    def headers(self,status):
        self.status=int(status);self.header_seconds=time.monotonic()-self.started
    def received(self,count):
        self.bytes+=count
        if self.monitor:
            with self.monitor.lock:self.monitor.inflight_bytes=self.bytes


@contextmanager
def request_timing(provider):
    if provider not in PROVIDERS:provider='Geodatendienst'
    monitor=_active.get();request=RequestTiming(monitor,provider)
    if monitor:
        with monitor.lock:
            monitor.inflight=provider;monitor.inflight_since=time.monotonic();monitor.inflight_bytes=0
    failed=False
    try:yield request
    except BaseException:
        failed=True;raise
    finally:
        if monitor:
            with monitor.lock:
                record=monitor.provider(provider);elapsed=time.monotonic()-request.started
                record['requests']+=1;record['bytes']+=request.bytes;record['seconds']+=elapsed
                record['max_seconds']=max(record['max_seconds'],elapsed)
                record['header_seconds']+=request.header_seconds or 0
                record['header_samples']+=int(request.header_seconds is not None)
                record['transfer_seconds']+=max(0,elapsed-request.header_seconds) if request.header_seconds is not None else 0
                record['errors']+=int(failed);record['last_status']=request.status
                if request.status in (429,502,503,504):record['temporary_errors']+=1
                monitor.inflight=None;monitor.inflight_bytes=0


def resource_limits(process,cgroup=Path('/sys/fs/cgroup')):
    try:cores=float(len(process.cpu_affinity()))
    except (AttributeError,OSError):cores=float(os.cpu_count() or 1)
    memory=None
    try:
        quota,period=(cgroup/'cpu.max').read_text().split()
        if quota!='max':cores=min(cores,int(quota)/int(period))
    except (OSError,ValueError,ZeroDivisionError):
        try:
            quota=int((cgroup/'cpu/cpu.cfs_quota_us').read_text());period=int((cgroup/'cpu/cpu.cfs_period_us').read_text())
            if quota>0:cores=min(cores,quota/period)
        except (OSError,ValueError,ZeroDivisionError):pass
    for path in (cgroup/'memory.max',cgroup/'memory/memory.limit_in_bytes'):
        try:
            value=int(path.read_text())
            if 0<value<2**60:memory=value;break
        except (OSError,ValueError):pass
    return max(.01,cores),memory


class ResourceSampler:
    def __init__(self):
        import psutil
        self.psutil=psutil;self.process=psutil.Process();self.previous={};self.last=time.monotonic()
        try:self.cores,self.limit=resource_limits(self.process)
        except psutil.Error:self.cores,self.limit=float(os.cpu_count() or 1),None
        self.peak=0;self.peak_cpu=0
    def sample(self):
        now=time.monotonic();current={};rss=0;delta=0
        try:processes=[self.process,*self.process.children(recursive=True)]
        except self.psutil.Error:processes=[self.process]
        for process in processes:
            try:
                key=(process.pid,process.create_time());cpu=sum(process.cpu_times()[:2])
                current[key]=cpu;rss+=process.memory_info().rss
                if key in self.previous:delta+=max(0,cpu-self.previous[key])
            except self.psutil.Error:continue
        percent=min(100.,100*delta/max(now-self.last,1e-6)/self.cores) if self.previous else None
        self.previous=current;self.last=now;self.peak=max(self.peak,rss);self.peak_cpu=max(self.peak_cpu,percent or 0)
        return {'cpu_percent':percent,'cpu_capacity_cores':self.cores,'rss_bytes':rss,
                'memory_limit_bytes':self.limit,'peak_rss_bytes':self.peak,'peak_cpu_percent':self.peak_cpu,
                'scope':'builder_and_renderers'}


class BuildMonitor:
    def __init__(self,folder,interval=1.):
        self.folder=Path(folder);self.interval=interval;self.lock=threading.RLock();self.stop=threading.Event()
        self.started=self.changed=time.monotonic();self.current='prepare';self.totals={key:0. for key in PHASES}
        self.providers={};self.retries=0;self.inflight=None;self.inflight_since=0.;self.inflight_bytes=0
        self.status='running';self.resources=None;self.sampler=None;self.previous_attempts=0;self.previous_seconds=0.
        previous=read_metrics(self.folder,'failed')
        if previous:
            self.previous_attempts=previous.get('attempt',1);self.previous_seconds=previous.get('total_elapsed_s',0.)
    def provider(self,name):
        if name not in PROVIDERS:name='Geodatendienst'
        return self.providers.setdefault(name,{'name':name,'requests':0,'cache_hits':0,'bytes':0,'seconds':0.,
            'header_seconds':0.,'header_samples':0,'transfer_seconds':0.,'max_seconds':0.,
            'errors':0,'temporary_errors':0,'last_status':None})
    def phase(self,key):
        if key not in PHASES:return
        with self.lock:
            now=time.monotonic();self.totals[self.current]+=now-self.changed;self.changed=now;self.current=key
    def snapshot(self):
        with self.lock:
            now=time.monotonic();totals=dict(self.totals);totals[self.current]+=now-self.changed
            elapsed=now-self.started
            return {'schema':1,'attempt':self.previous_attempts+1,'elapsed_s':elapsed,
                'total_elapsed_s':self.previous_seconds+elapsed,'status':self.status,'updated_at':time.time(),
                'phase':self.current,'phases':[{'key':k,'name':PHASES[k],'seconds':v} for k,v in totals.items()],
                'providers':[dict(v) for v in self.providers.values()], 'retries':self.retries,
                'inflight':({'name':self.inflight,'seconds':now-self.inflight_since,'bytes':self.inflight_bytes} if self.inflight else None),
                'resources':self.resources}
    def write(self):
        # Metrics never break or stall a track export. One small atomic write/s.
        path=self.folder/'telemetry.json';temp=path.with_suffix('.tmp')
        try:
            temp.write_text(json.dumps(self.snapshot(),allow_nan=False),encoding='utf-8');temp.replace(path)
        except (OSError,ValueError):pass
    def _sample(self):
        while not self.stop.wait(self.interval):
            try:
                if self.sampler:self.resources=self.sampler.sample()
            except (OSError,RuntimeError):self.resources=None
            self.write()
    def __enter__(self):
        self.token=_active.set(self)
        try:self.sampler=ResourceSampler();self.resources=self.sampler.sample()
        except (ImportError,OSError,RuntimeError):pass
        self.write();self.thread=threading.Thread(target=self._sample,name='build-metrics',daemon=True);self.thread.start()
        return self
    def __exit__(self,kind,value,traceback):
        self.stop.set();self.thread.join(timeout=3);self.status='failed' if kind else 'succeeded';self.write();_active.reset(self.token)


def read_metrics(folder,status):
    path=Path(folder)/'telemetry.json'
    try:
        if path.stat().st_size>65536:return None
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('schema')!=1:return None
        data['live']=status=='running' and 0<=time.time()-float(data['updated_at'])<6 and data.get('status')=='running'
        return data
    except (OSError,ValueError,KeyError,TypeError):return None
