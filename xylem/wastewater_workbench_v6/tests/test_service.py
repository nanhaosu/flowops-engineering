"""Exercise complete state transitions and persistence, not mocked recommendations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from xylem.wastewater_workbench_v6.service import Workbench, APIError
except ImportError:
    Workbench = None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Workbench, 'v6 transactional service is required')
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'session.json'
        self.app = Workbench(self.path)

    def act(self, kind, **fields):
        return self.app.action(dict(action=kind,expected_revision=self.app.state()['revision'],**fields))

    def bundle(self):
        return dict(rates={}, transfers=[dict(direction='store',firm='A',volume=5)])

    def test_public_export_excludes_private_future(self):
        state = self.app.state()
        self.assertEqual(state,self.app.state())
        for key in ('"scenario"','"disturbances"','"_physical_capacity"','"world_events"'):
            self.assertNotIn(key,json.dumps(self.app.export()))
        self.assertIn('disposition',state['simulation'])

    def test_exact_bundle_preflight_and_stale_revision(self):
        draft = self.bundle()
        before = self.app.state()
        preview = self.act('preview',**draft)['preview']
        self.assertTrue(preview['allowed'],preview)
        changed = self.bundle();changed['transfers'][0]['volume']=6
        with self.assertRaises(APIError):
            self.act('publish',**changed,preview_token=preview['token'])
        self.assertEqual(before,self.app.state())
        result = self.act('publish',**draft,preview_token=preview['token'])
        self.assertEqual(result['revision'],before['revision']+1)
        with self.assertRaises(APIError):
            self.app.action(dict(action='step',steps=1,expected_revision=before['revision']))
        with self.assertRaises(APIError):
            self.act('publish',**draft,preview_token=preview['token'])

    def test_unknown_destination_refuses_without_stock_disappearing(self):
        self.act('confirmation',destination='storage',known=False,valid_for_minutes=60)
        before=self.app.state()
        p=self.act('preview',**self.bundle())['preview']
        self.assertFalse(p['allowed']);self.assertIsNone(p['token'])
        self.assertEqual(before,self.app.state())

    def test_disk_failure_does_not_commit_or_consume_preview(self):
        draft=self.bundle();token=self.act('preview',**draft)['preview']['token']
        before=self.app.state()
        with patch.object(self.app,'_persist',side_effect=OSError('disk full')):
            with self.assertRaises(APIError):self.act('publish',**draft,preview_token=token)
        self.assertEqual(before,self.app.state())
        self.act('publish',**draft,preview_token=token)

    def test_serialized_reopen_and_branch_restore_preserve_liability(self):
        draft=self.bundle();token=self.act('preview',**draft)['preview']['token']
        self.act('publish',**draft,preview_token=token)
        self.act('step',steps=4)
        before=self.app.state()['simulation']
        reopened=Workbench(self.path)
        self.assertEqual(before,reopened.state()['simulation'])
        saved=self.act('fork',name='return responsibility')
        branch=saved['branches'][-1]['id']
        self.act('step',steps=4)
        restored=self.act('restore',branch_id=branch)
        self.assertEqual(before,restored['simulation'])

    def test_invalid_and_nonfinite_inputs_leave_session_unchanged(self):
        before=self.app.state()
        for fields in (dict(rates={},transfers=[dict(direction='store',firm='A',volume=float('nan'))]),
                       dict(rates={},transfers=[dict(direction='store',firm='A',volume=True)]),
                       dict(rates={},transfers=[dict(direction='store',firm='A',volume=-1)])):
            with self.assertRaises(APIError):self.act('preview',**fields)
        self.assertEqual(before,self.app.state())


if __name__=='__main__':unittest.main()
