"""A single bounded builder with persistent history and isolated output directories."""
from datetime import datetime,timezone
import json
import os
import signal
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from webapp.catalog import ROOT,validate_request
from webapp.geodata import GeoService

def now():
    return datetime.now(timezone.utc).isoformat()

class QueueFull(ValueError):
    pass

def terminate_builder(process):
    """Stop this worker and its renderer, without touching other Blender sessions."""
    if process.poll() is not None:
        return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW,timeout=15,check=False)
    else:
        try:
            os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name!='nt':
            os.killpg(process.pid,signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)

class JobManager:
    def __init__(self,data_root=None,start_worker=True,geo_service=None):
        self.root=Path(data_root or ROOT/'web-data').resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.geo=geo_service if geo_service is not None else GeoService(self.root)
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.wake=threading.Event()
        self.process=None
        # OS lock prevents two local servers from consuming the same queue.
        self.instance=(self.root/'server.lock').open('a+b')
        self.instance.seek(0);self.instance.write(b'0');self.instance.flush();self.instance.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.instance.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.instance.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.instance.close()
            raise RuntimeError('Für diesen Datenordner läuft bereits ein Streckenbauer.')
        self.db=sqlite3.connect(self.root/'jobs.sqlite3',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL, error TEXT, summary TEXT)')
        self.db.execute("UPDATE jobs SET status='failed',stage='Unterbrochen',error='Der Dienst wurde während des Baus beendet. Bitte erneut erstellen.' WHERE status='running'")
        self.db.commit()
        self.worker=None
        if start_worker:
            self.worker=threading.Thread(target=self._run,name='track-builder',daemon=True)
            self.worker.start()

    def close(self):
        self.stop.set();self.wake.set()
        if self.worker:
            self.worker.join(timeout=25)
        if self.process and self.process.poll() is None:
            terminate_builder(self.process)
        if self.worker:
            self.worker.join(timeout=5)
        with self.lock:
            self.db.close()
            self.instance.close()

    def _public(self,row):
        request=json.loads(row['request'])
        ready=row['status']=='succeeded'
        stored=json.loads(row['summary']) if row['summary'] else None
        # Older exports contain megabytes of per-building geometry here.
        # Poll only display data; complete evidence remains in each ZIP.
        summary=None
        if stored:
            keys=('id','name','version','length_m','buildings','elevation_gain_m',
                  'elevation_min_m','elevation_max_m','hillside_retaining_assemblies',
                  'hillside_staircases','hillside_driveways','preview_kind','preview_views')
            summary={key:stored[key] for key in keys if key in stored}
            if isinstance(stored.get('ai_materials'),dict):
                summary['ai_materials']={'enabled':stored['ai_materials'].get('enabled',False)}
            if isinstance(stored.get('ai_generation'),dict):
                summary['ai_generation']={k:v for k,v in stored['ai_generation'].items() if k in ('cache_hit','usage')}
            if isinstance(stored.get('source_route'),dict):
                summary['source_route']={k:v for k,v in stored['source_route'].items() if k in ('id','name','length_m','source','warnings')}
        return {'id':row['id'],'name':request['name'],'created_at':row['created_at'],
                'status':row['status'],'stage':row['stage'],'progress':row['progress'],
                'error':row['error'],'settings':request,
                'summary':summary,
                'download_url':f'/api/jobs/{row["id"]}/download' if ready else None,
                'preview_url':f'/api/jobs/{row["id"]}/preview.png' if ready else None,
                'preview_urls':({view:f'/api/jobs/{row["id"]}/'+('preview' if view=='overview' else view)+'.png'
                                 for view in stored.get('preview_views',['overview']) if view in ('overview','driver','environment')}
                                if ready and row['summary'] else {})}

    def list(self):
        with self.lock:
            return [self._public(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100')]

    def get(self,job_id):
        with self.lock:
            row=self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            return self._public(row) if row else None

    def create(self,value):
        request=validate_request(value,data_root=self.root,geo_service=self.geo)
        with self.lock:
            pending=self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if pending>=5:
                raise QueueFull('Die Warteschlange ist voll. Bitte einen laufenden Bau abwarten.')
            job_id=uuid.uuid4().hex
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',(job_id,now(),json.dumps(request,ensure_ascii=False),'queued','Wartet',0,None,None))
            self.db.commit()
            self.wake.set()
            return self.get(job_id)

    def _update(self,job_id,**values):
        with self.lock:
            self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),job_id))
            self.db.commit()

    def artifact(self,job_id,kind):
        job=self.get(job_id)
        if not job or job['status']!='succeeded':
            return None
        folder=self.root/job_id
        result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
        if kind not in result:return None
        p=(folder/result[kind]).resolve()
        return p if p.is_relative_to(folder) and p.is_file() else None

    def _run(self):
        while not self.stop.is_set():
            with self.lock:
                row=self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                self.wake.wait(1);self.wake.clear();continue
            job_id=row['id'];folder=self.root/job_id
            self._update(job_id,status='running',stage='Quelldaten vorbereiten',progress=3)
            try:
                folder.mkdir(exist_ok=True)
                (folder/'request.json').write_text(row['request'],encoding='utf-8')
                with (folder/'build.log').open('w',encoding='utf-8') as log:
                    kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                    self.process=subprocess.Popen([sys.executable,'-u','-m','webapp.worker','--job-dir',str(folder)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,**kwargs)
                    began=time.monotonic()
                    while self.process.poll() is None:
                        if self.stop.wait(.5):
                            terminate_builder(self.process)
                            raise RuntimeError('Dienst beendet.')
                        settings=json.loads(row['request'])
                        timeout=1800 if settings.get('template_id')=='hessen' else (1200 if settings.get('ai_materials') else 900)
                        if time.monotonic()-began>timeout:
                            terminate_builder(self.process)
                            raise RuntimeError('Der Bau hat das Zeitlimit überschritten.')
                        progress=folder/'progress.json'
                        if progress.exists():
                            try:
                                value=json.loads(progress.read_text(encoding='utf-8'))
                                self._update(job_id,stage=value['stage'],progress=value['progress'])
                            except (OSError,ValueError,KeyError):
                                pass
                    if self.process.returncode:
                        if (folder/'error.json').is_file():
                            error=json.loads((folder/'error.json').read_text(encoding='utf-8'))
                            raise RuntimeError(str(error.get('error','KI-Anfrage fehlgeschlagen.'))[:400])
                        raise RuntimeError('Die Strecke konnte nicht erstellt werden. Details stehen im lokalen Bauprotokoll.')
                result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
                self._update(job_id,status='succeeded',stage='Fertig',progress=100,summary=json.dumps(result['summary'],ensure_ascii=False))
            except Exception as error:
                self._update(job_id,status='failed',stage='Fehlgeschlagen',error=str(error)[:400])
            finally:
                self.process=None
