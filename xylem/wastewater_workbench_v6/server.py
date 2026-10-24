"""Python stdlib loopback-only HTTP server for the local recovery workbench."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

from .service import Workbench, APIError, HERE

MAX_BODY = 65536
STATIC = {'/':'index.html','/index.html':'index.html',
          '/static/model.js':'model.js','/static/app.js':'app.js','/static/production.js':'production.js','/static/style.css':'style.css',
          '/app.js':'app.js','/style.css':'style.css'}
MIME = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}


def _reject_constant(value):
    raise ValueError('Non-finite JSON numbers are not accepted')


class Handler(BaseHTTPRequestHandler):
    server_version = 'DispositionWorkbench/6'

    def log_message(self,format,*args):
        pass

    def _send(self,status,body,content_type='application/json; charset=utf-8',download=False):
        raw = body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download: self.send_header('Content-Disposition','attachment; filename="recovery-workbench-v5-public.json"')
        self.end_headers()
        self.wfile.write(raw)

    def _error(self,error):
        with self.server.workbench.lock:
            revision = self.server.workbench.data['revision']
        self._send(error.status,dict(error=dict(code=error.code,message=error.message),revision=revision))

    def _local(self):
        try:
            host = urlsplit('http://'+self.headers.get('Host',''))
            if host.hostname not in ('127.0.0.1','localhost') or host.port != self.server.server_port:
                raise ValueError()
            origin = self.headers.get('Origin')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.port!=self.server.server_port:
                    raise ValueError()
            if self.headers.get('Sec-Fetch-Site')=='cross-site': raise ValueError()
        except ValueError:
            raise APIError('local_only','This workbench accepts same-origin loopback requests only',403)

    def do_GET(self):
        try:
            self._local()
            path = urlsplit(self.path).path
            app = self.server.workbench
            endpoints = {'/api/state':app.state,'/api/catalog':app.catalog,'/api/evidence':app.evidence,'/api/export':app.export}
            if path in endpoints:
                self._send(200,endpoints[path](),download=path=='/api/export')
            elif path in STATIC:
                file = HERE/'static'/STATIC[path]
                if not file.is_file(): raise APIError('not_found','Static asset is not installed',404)
                self._send(200,file.read_bytes(),MIME[file.suffix])
            else: raise APIError('not_found','Unknown endpoint',404)
        except APIError as exc: self._error(exc)
        except (OSError,ValueError) as exc: self._error(APIError('read_failed','Could not read local resource',500))

    def do_POST(self):
        try:
            self._local()
            if urlsplit(self.path).path!='/api/action': raise APIError('not_found','Unknown endpoint',404)
            if self.headers.get_content_type()!='application/json': raise APIError('content_type','Use application/json',415)
            length = self.headers.get('Content-Length')
            if not length or not length.isdigit(): raise APIError('invalid_body','Content-Length is required')
            length = int(length)
            if not 0<length<=MAX_BODY: raise APIError('invalid_body','Body must be 1..65536 bytes',413)
            try:
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body)!=length: raise ValueError('Incomplete body')
                payload = json.loads(body.decode('utf-8'),parse_constant=_reject_constant)
            except (ValueError,UnicodeError,OSError) as exc:
                raise APIError('invalid_json','Body must be valid finite JSON') from exc
            self._send(200,self.server.workbench.action(payload))
        except APIError as exc: self._error(exc)
        except (TypeError,ValueError) as exc: self._error(APIError('invalid_input','Invalid action input'))


def make_server(workbench,host='127.0.0.1',port=8781):
    if host not in ('127.0.0.1','localhost'): raise ValueError('Loopback binding required')
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.workbench = workbench
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1',choices=('127.0.0.1','localhost'))
    parser.add_argument('--port',default=8781,type=int)
    parser.add_argument('--state',type=Path,default=HERE/'runtime/current.json')
    args = parser.parse_args()
    server = make_server(Workbench(args.state),host=args.host,port=args.port)
    print('Disposition workbench v6 http://127.0.0.1:'+str(server.server_port)+'/',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()


