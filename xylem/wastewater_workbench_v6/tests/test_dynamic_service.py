import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

if importlib.util.find_spec('xylem.wastewater_workbench_v6.scenarios'):
    from xylem.wastewater_workbench_v6 import scenarios, engine
    from xylem.wastewater_workbench_v6.service import Workbench, APIError
else:
    scenarios = Workbench = None


class DynamicServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(scenarios, 'dynamic scenarios/service are not implemented')
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.json'
        self.app=Workbench(self.path)

    def act(self, action, **fields):
        return self.app.action(dict(action=action,expected_revision=self.app.state()['revision'],**fields))

    def test_production_pause_is_part_of_exact_atomic_bundle(self):
        draft=dict(rates={},transfers=[],production={'B':False})
        p=self.act('preview',**draft)['preview']
        self.assertTrue(p['allowed'],p)
        before=self.app.state()
        with self.assertRaises(APIError):
            self.act('publish',rates={},transfers=[],production={'B':True},preview_token=p['token'])
        self.assertEqual(before,self.app.state())
        r=self.act('publish',**draft,preview_token=p['token'])
        self.assertEqual(len(r['simulation']['orders']),1)
        self.assertEqual(r['simulation']['orders'][0]['kind'],'production')

    def test_invalid_production_rolls_back_transfer_and_rate_together(self):
        draft=dict(rates={},transfers=[dict(direction='store',firm='A',volume=5)],production={'A':False})
        before=self.app.state()
        p=self.act('preview',**draft)['preview']
        self.assertFalse(p['allowed'])
        self.assertEqual(before,self.app.state())

    def test_saved_reopen_keeps_tail_and_manufacturing_debt(self):
        d=dict(rates={},transfers=[],production={'B':False})
        p=self.act('preview',**d)['preview']
        self.act('publish',**d,preview_token=p['token'])
        self.act('step',steps=16)
        s=self.app.state()['simulation']
        self.assertGreater(s['production_control']['metrics']['unmet_production_units'],0)
        self.assertEqual(s,Workbench(self.path).state()['simulation'])

    def test_owner_confirmation_invalidates_preflight(self):
        draft=dict(rates={},transfers=[],production={'B':False})
        p=self.act('preview',**draft)['preview']
        self.act('production_confirmation',firm='B',owner='production_owner_B',authorized=False,valid_for_minutes=120)
        with self.assertRaises(APIError):
            self.act('publish',**draft,preview_token=p['token'])
        self.assertFalse(self.act('preview',**draft)['preview']['allowed'])

    def test_unknown_tail_case_does_not_offer_a_free_pause(self):
        self.act('reset',case_id='maintenance_delay',resource_enabled=True,variant='unknown_tail')
        p=self.act('preview',rates={},transfers=[],production={'B':False})['preview']
        self.assertFalse(p['allowed'])
        self.assertIn('production:B:unknown_residual_minutes',p['reasons'])

    def test_public_export_and_forecast_hide_future_events(self):
        text=json.dumps(self.app.export())
        for key in ('"scenario"','"disturbances"','"world_events"','"_physical_capacity"'):
            self.assertNotIn(key,text)
        cloned=engine.from_public(self.app.state()['simulation'])
        self.assertEqual(cloned['scenario']['disturbances'],[])
        self.assertIn('production_control',engine.public_state(cloned))

    def test_preflight_exposes_output_cost_before_publishing(self):
        p=self.act('preview',rates={},transfers=[],production={'B':False})['preview']
        self.assertTrue(p['allowed'])
        self.assertEqual(p['conditional_4h']['status'],'conditional_simulation')
        self.assertAlmostEqual(p['conditional_4h']['selected']['unmet_production_units'],200)
        self.assertAlmostEqual(p['conditional_4h']['selected']['tail_generated_m3'],4.0625)
        self.assertEqual(p['conditional_4h']['hold_current']['unmet_production_units'],0)
        self.assertEqual(self.app.state()['simulation']['minute'],0)
        self.assertEqual(self.app.state()['simulation']['orders'],[])


if __name__=='__main__':unittest.main()
