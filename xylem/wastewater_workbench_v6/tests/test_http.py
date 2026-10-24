import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from xylem.wastewater_workbench_v6.service import Workbench
try:
    from xylem.wastewater_workbench_v6.server import make_server
except ImportError:
    make_server=None

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(make_server,'v5 server required')
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.server=make_server(Workbench(Path(self.tmp.name)/'state.json'),port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        c.request(method,path,body=body,headers=headers or {});r=c.getresponse();result=r.status,r.read();c.close();return result
    def test_public_state_and_stale_write(self):
        status,raw=self.request('GET','/api/state');self.assertEqual(status,200)
        self.assertIn('disposition',json.loads(raw)['simulation'])
        body=json.dumps(dict(action='step',steps=1,expected_revision=100))
        status,raw=self.request('POST','/api/action',body,{'Content-Type':'application/json'})
        self.assertEqual(status,409);self.assertEqual(json.loads(raw)['error']['code'],'stale_revision')
    def test_origin_static_allowlist_and_nonfinite(self):
        self.assertEqual(self.request('GET','/api/state',headers={'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.request('GET','/../service.py')[0],404)
        body='{"action":"preview","expected_revision":0,"rates":{"A":NaN},"transfers":[]}'
        self.assertEqual(self.request('POST','/api/action',body,{'Content-Type':'application/json'})[0],400)

if __name__=='__main__':unittest.main()
