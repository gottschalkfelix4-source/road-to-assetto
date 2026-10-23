"""Per-owner build diagnostics, real acquisition hooks and container accounting."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from webapp.geodata import GeoService, GeoError, DGM
from webapp.telemetry import (BuildMonitor, ResourceSampler, cache_hit, read_metrics,
                             request_timing, resource_limits, retry_wait, set_phase)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_request_body_and_failure_are_measured_without_urls(self):
        response=MagicMock();response.__enter__.return_value=response
        response.status_code=200;response.headers={'Content-Length':'6'}
        response.iter_content.return_value=[b'abc',b'def']
        session=MagicMock();session.__enter__.return_value=session;session.request.return_value=response
        with BuildMonitor(self.root) as monitor, patch('webapp.geodata.requests.Session',return_value=session):
            service=GeoService(self.root)
            self.assertEqual(service._request_bytes(DGM,params={'private':'never-log-this'}),b'abcdef')
            response.status_code=429
            with self.assertRaises(GeoError):service._request_bytes(DGM)
            cache_hit('Hessen DGM1')
            set_phase('model')
            with retry_wait(10):self.assertEqual(monitor.snapshot()['phase'],'wait')
            self.assertEqual(monitor.snapshot()['phase'],'model')
        saved=read_metrics(self.root,'succeeded');p=saved['providers'][0]
        self.assertEqual((p['requests'],p['bytes'],p['cache_hits'],p['errors'],p['temporary_errors']),(2,6,1,1,1))
        self.assertEqual(p['last_status'],429);self.assertEqual(saved['retries'],1)
        self.assertFalse(saved['live']);self.assertIsNone(saved['inflight'])
        self.assertNotIn('never-log-this',json.dumps(saved));self.assertNotIn('https:',json.dumps(saved))
        self.assertAlmostEqual(sum(p['seconds'] for p in saved['phases']),saved['elapsed_s'],places=5)

    def test_failed_request_and_attempt_total_survive_resume(self):
        with self.assertRaises(RuntimeError):
            with BuildMonitor(self.root):
                with request_timing('https://private.invalid/?secret=hidden'):
                    raise RuntimeError('failure')
        first=read_metrics(self.root,'failed')
        self.assertEqual(first['status'],'failed');self.assertEqual(first['providers'][0]['name'],'Geodatendienst')
        with BuildMonitor(self.root):pass
        second=read_metrics(self.root,'succeeded')
        self.assertEqual(second['attempt'],2)
        self.assertAlmostEqual(second['total_elapsed_s'],first['elapsed_s']+second['elapsed_s'])

    def test_missing_stale_and_invalid_records(self):
        self.assertIsNone(read_metrics(self.root,'running'))
        with BuildMonitor(self.root):self.assertTrue(read_metrics(self.root,'running')['live'])
        path=self.root/'telemetry.json';data=json.loads(path.read_text())
        data.update(status='running',updated_at=time.time()-60);path.write_text(json.dumps(data))
        self.assertFalse(read_metrics(self.root,'running')['live'])
        path.write_text('{');self.assertIsNone(read_metrics(self.root,'running'))
        path.write_text(' '*65537);self.assertIsNone(read_metrics(self.root,'running'))

    def test_monitor_failure_cannot_break_build(self):
        with patch('webapp.telemetry.ResourceSampler',side_effect=OSError('unavailable')):
            with BuildMonitor(self.root/'missing') as monitor:set_phase('model')
        self.assertIsNone(monitor.resources)

    def test_cgroup_limits_are_honoured(self):
        process=MagicMock();process.cpu_affinity.return_value=list(range(16))
        (self.root/'cpu.max').write_text('150000 100000')
        (self.root/'memory.max').write_text(str(1024**3))
        self.assertEqual(resource_limits(process,self.root),(1.5,1024**3))
        (self.root/'cpu.max').write_text('max 100000')
        (self.root/'memory.max').write_text('max')
        self.assertEqual(resource_limits(process,self.root),(16.,None))

    def test_cpu_is_normalized_and_renderer_memory_included(self):
        import psutil
        parent=MagicMock();child=MagicMock();parent.children.return_value=[child]
        for process,pid in ((parent,1),(child,2)):
            process.pid=pid;process.create_time.return_value=pid
            process.cpu_times.return_value=(1.,0.);process.memory_info.return_value.rss=1000
        with patch('psutil.Process',return_value=parent),patch('webapp.telemetry.resource_limits',return_value=(2.,None)),patch('webapp.telemetry.time.monotonic',side_effect=[0.,0.,1.,2.]):
            sampler=ResourceSampler();self.assertIsNone(sampler.sample()['cpu_percent'])
            parent.cpu_times.return_value=(1.5,0.);child.cpu_times.return_value=(1.5,0.)
            measured=sampler.sample()
            self.assertEqual(measured['cpu_percent'],50.);self.assertEqual(measured['rss_bytes'],2000)
            child.cpu_times.side_effect=psutil.NoSuchProcess(2)
            self.assertEqual(sampler.sample()['rss_bytes'],1000)


if __name__=='__main__':unittest.main()
