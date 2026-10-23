"""Dependency-free local HTTP interface for the track builder."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import mimetypes
import re
import signal
import threading
from urllib.parse import urlsplit
from webapp.catalog import ROOT,config,template
from webapp.jobs import JobManager,QueueFull
from webapp import ai
from webapp.geodata import GeoError

def public_route(route):
    """Never return stored provider responses or local source-cache paths."""
    return {key:route[key] for key in ('id','name','length_m','coordinates','bounds','source','warnings','start','end') if key in route}


def map_config():
    from html import escape
    url=os.environ.get('ROADTOASSETTO_TILE_URL','https://tile.openstreetmap.org/{z}/{x}/{y}.png')
    parsed=urlsplit(url)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or any(c in parsed.netloc for c in ('{','}','\r','\n',' '))
            or not all(key in url for key in ('{z}','{x}','{y}'))):
        raise ValueError('ROADTOASSETTO_TILE_URL must be an HTTP(S) tile URL with {z}/{x}/{y}, without credentials.')
    attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    extra=os.environ.get('ROADTOASSETTO_TILE_ATTRIBUTION','').strip()
    if extra:attribution+=' · '+escape(extra)
    return {'tile_url':url,'attribution':attribution,'tile_origin':f'{parsed.scheme}://{parsed.netloc}'}


def create_server(host='127.0.0.1',port=8765,data_root=None,start_worker=True,geo_service=None):
    tiles=map_config()
    manager=JobManager(data_root,start_worker=start_worker,geo_service=geo_service)
    class Handler(BaseHTTPRequestHandler):
        server_version='RoadToAssetto/0.12'
        def log_message(self,fmt,*args):
            # URLs contain only local template/job identifiers, never submitted names.
            print('%s %s'%(self.log_date_time_string(),fmt%args),flush=True)

        def send_payload_headers(self,status,kind,length,filename=None):
            self.send_response(status)
            self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(length))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
            self.send_header('Content-Security-Policy',f"default-src 'self'; img-src 'self' data: {tiles['tile_origin']}; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
            self.end_headers()

        def json(self,value,status=200):
            data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_payload_headers(status,'application/json; charset=utf-8',len(data))
            self.wfile.write(data)

        def file(self,path,download=False):
            if not path or not path.is_file():
                return self.json({'error':'Datei nicht gefunden.'},404)
            self.send_payload_headers(200,mimetypes.guess_type(path.name)[0] or 'application/octet-stream',path.stat().st_size,path.name if download else None)
            with path.open('rb') as stream:
                while block:=stream.read(256*1024):
                    self.wfile.write(block)

        def valid_host(self):
            host_value=self.headers.get('Host','')
            try:
                hostname=urlsplit('//'+host_value).hostname
            except ValueError:
                hostname=None
            allowed={'127.0.0.1','localhost','::1'}
            if host in ('127.0.0.1','localhost','::1') and hostname not in allowed:
                self.json({'error':'Unzulässiger Host.'},403);return False
            return True

        def do_GET(self):
            if not self.valid_host(): return
            path=urlsplit(self.path).path
            if path=='/api/health':
                return self.json({'status':'ok','version':config()['version'],'mode':'local','templates':['altenhain'],
                                  'features':{'interactive_map':True,'address_search':True,'hessen_routes':True}})
            if path=='/api/catalog': return self.json({'templates':[template()],'map_config':{k:v for k,v in tiles.items() if k!='tile_origin'},
                'discovery':{'region':'Hessen','min_segment_m':120,'max_length_m':2000,'max_area_km2':2,
                             'bounds':[7.7,49.35,10.3,51.7]}})
            if path=='/api/jobs': return self.json({'jobs':manager.list()})
            if path=='/api/ai/settings':
                try:return self.json(ai.public_settings(manager.root))
                except ai.AIError as error:return self.json({'error':error.message,'code':error.code},error.status)
            if path=='/api/templates/altenhain/map.jpg':
                return self.file(ROOT/config()['data_dir']/'textures/ortho.jpg')
            if path=='/api/templates/altenhain/preview.png':
                return self.file(ROOT/'build'/config()['id']/'ui/preview.png')
            route_match=re.fullmatch(r'/api/routes/([a-f0-9]{32})',path)
            if route_match:
                try:return self.json({'route':public_route(manager.geo.get_route(route_match[1]))})
                except GeoError as error:return self.json({'error':str(error),'code':error.code},error.status)
            match=re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(download|preview\.png|driver\.png|environment\.png))?',path)
            if match:
                job_id,artifact=match.groups()
                if artifact:
                    return self.file(manager.artifact(job_id,'archive' if artifact=='download' else artifact.removesuffix('.png')),download=artifact=='download')
                job=manager.get(job_id)
                return self.json(job if job else {'error':'Auftrag nicht gefunden.'},200 if job else 404)
            static={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/map.js':'map.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}
            if path in static:
                return self.file(ROOT/'webapp/static'/static[path])
            if re.fullmatch(r'/vendor/leaflet/(leaflet\.(js|css)|images/(layers(?:-2x)?|marker-icon(?:-2x)?|marker-shadow)\.png)',path):
                return self.file(ROOT/'webapp/static'/path.lstrip('/'))
            return self.json({'error':'Nicht gefunden.'},404)

        def do_POST(self):
            if not self.valid_host():return
            origin=self.headers.get('Origin')
            if origin and origin!='http://'+self.headers.get('Host','') and origin!='https://'+self.headers.get('Host',''):
                return self.json({'error':'Anfrage von einer anderen Webseite abgelehnt.'},403)
            path=urlsplit(self.path).path
            if path not in ('/api/jobs','/api/ai/settings','/api/ai/test','/api/search','/api/routes/preview'):
                return self.json({'error':'Nicht gefunden.'},404)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                return self.json({'error':'JSON erwartet.'},415)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16384:
                    return self.json({'error':'Anfrage ist zu groß oder leer.'},413)
                data=json.loads(self.rfile.read(size).decode('utf-8'))
                if path=='/api/search':
                    if not isinstance(data,dict) or set(data)!={'query'}:
                        raise ValueError('Bitte einen Suchbegriff angeben.')
                    return self.json({'results':manager.geo.search(data['query'])})
                if path=='/api/routes/preview':
                    if not isinstance(data,dict) or set(data)!={'start','end'}:
                        raise ValueError('Bitte Start und Ziel auf der Karte setzen.')
                    return self.json({'route':public_route(manager.geo.preview(data['start'],data['end']))})
                if path=='/api/ai/settings':return self.json(ai.save_settings(manager.root,data))
                if path=='/api/ai/test':return self.json(ai.test_connection(manager.root))
                if isinstance(data,dict) and data.get('ai_materials'):
                    settings=ai.public_settings(manager.root)
                    if not settings.get('image_model'):
                        raise ValueError('Bitte zuerst einen KI-Anbieter und ein Bildmodell speichern.')
                return self.json(manager.create(data),202)
            except ai.AIError as error:
                return self.json({'error':error.message,'code':error.code},error.status)
            except GeoError as error:
                return self.json({'error':str(error),'code':error.code},error.status)
            except QueueFull as error:
                return self.json({'error':str(error)},429)
            except (ValueError,UnicodeError) as error:
                return self.json({'error':str(error)},400)

    try:
        server=ThreadingHTTPServer((host,port),Handler)
    except Exception:
        manager.close()
        raise
    server.manager=manager
    return server

def main():
    parser=argparse.ArgumentParser(description='Road to Assetto lokal starten')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir')
    args=parser.parse_args()
    server=create_server(args.host,args.port,args.data_dir)
    # shutdown() must run outside serve_forever's own thread. Docker sends TERM
    # to this process; the finally block also closes the owned builder process.
    def stop(signum,frame):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    print(f'Road to Assetto: http://{args.host}:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close();server.manager.close()

if __name__=='__main__':main()
