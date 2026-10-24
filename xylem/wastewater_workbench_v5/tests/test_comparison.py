"""Behavioral checks for resource attribution, information parity and terminal debt."""
import importlib.util
import unittest
from copy import deepcopy
import json
from pathlib import Path


class ScenarioContractTests(unittest.TestCase):
    def test_maintenance_revision_becomes_public_at_announcement_not_before(self):
        from xylem.wastewater_workbench_v5 import engine
        from xylem.wastewater_workbench_v5.scenarios import configuration
        sim=engine.new_simulation(configuration())
        first=engine.public_state(sim)
        self.assertEqual((first.get('maintenance_plan') or {}).get('expected_restore_minutes'),480)
        engine.advance(sim,24)
        self.assertEqual(engine.public_state(sim)['maintenance_plan']['expected_restore_minutes'],480)
        engine.advance(sim,1)
        view=engine.public_state(sim)
        self.assertEqual(view['maintenance_plan']['expected_restore_minutes'],720)
        self.assertEqual(view['recovery']['capacity_confirmation']['value'],25)
        self.assertEqual(sim['_physical_capacity'],25)

    def test_resource_toggle_does_not_change_production_or_disturbances(self):
        spec = importlib.util.find_spec('xylem.wastewater_workbench_v5.scenarios')
        self.assertIsNotNone(spec, 'v5 scenario configuration is not implemented')
        from xylem.wastewater_workbench_v5.scenarios import configuration
        a = configuration('maintenance_delay', resource_enabled=False)
        b = configuration('maintenance_delay', resource_enabled=True)
        self.assertEqual(a['firms'], b['firms'])
        self.assertEqual(a['disturbances'], b['disturbances'])
        self.assertEqual(a['notice_budget'], b['notice_budget'])
        self.assertFalse(a['disposition']['enabled'])
        self.assertTrue(b['disposition']['enabled'])
        a['disposition']['enabled'] = True
        self.assertEqual(a, b, 'resource assignment must change only availability')

    def test_observation_continues_after_recovery_with_production(self):
        spec = importlib.util.find_spec('xylem.wastewater_workbench_v5.scenarios')
        self.assertIsNotNone(spec, 'v5 scenario configuration is not implemented')
        from xylem.wastewater_workbench_v5.scenarios import configuration
        cfg = configuration('return_overlap')
        self.assertEqual(cfg['end_tick'], 192)
        self.assertEqual(cfg['production_end'], 192)
        last_recovery = max(e['minute'] for e in cfg['disturbances'] if e['kind'] == 'capacity')
        self.assertLess(last_recovery, 24 * 60)
        self.assertGreater(sum(f['production_rate'] for f in cfg['firms'].values()), 0)


class ControllerContractTests(unittest.TestCase):
    def test_planner_forecast_records_arrival_of_already_returning_water(self):
        from xylem.wastewater_workbench_v5 import engine,disposition,planning
        from xylem.wastewater_workbench_v5.tests.test_engine import fixture
        cfg=fixture();cfg['disposition']['initial_inventory']={'A':3}
        sim=engine.new_simulation(cfg)
        disposition.request(sim,'return','A',3)
        engine.advance(sim)
        self.assertAlmostEqual(sum(p['volume'] for p in sim['transit']),3)
        forecast=engine.conditional_run(planning._public_copy(engine.public_state(sim)),horizon=4)
        d=forecast['final_state']['disposition']
        self.assertAlmostEqual(d['metrics']['arrived_return_route_m3'],3)
        self.assertEqual(d['orders'][0]['status'],'complete')
        self.assertEqual(d['open_orders'],0)

    def test_planner_objective_keeps_in_transit_return_debt(self):
        from xylem.wastewater_workbench_v5 import engine,disposition,planning,controllers
        from xylem.wastewater_workbench_v5.tests.test_engine import fixture
        cfg=fixture();cfg['disposition']['initial_inventory']={'A':3}
        sim=engine.new_simulation(cfg)
        disposition.request(sim,'return','A',3)
        engine.advance(sim)
        view=planning._public_copy(engine.public_state(sim))
        clone=engine.from_public(view)
        self.assertEqual(controllers._objective(view,clone)[2],3)

    def test_simple_does_not_spend_notice_for_no_forecast_improvement(self):
        # Hand-checked r1 state: waiting and the 0.83333333m3 storage request
        # both forecast 0 overflow and 374.9166667m3 total inventory, but the
        # request adds 0.8333333m3 return debt and a notice. No future event used.
        from xylem.wastewater_workbench_v5.controllers import recommend
        path=Path(__file__).resolve().parents[1]/'artifacts/evidence/no_benefit_transfer_fixture.json'
        view=json.loads(path.read_text(encoding='utf-8'))
        action=recommend(view,'simple')
        self.assertEqual(action['transfers'],[])

    def test_no_resource_means_no_extra_strategy_privilege(self):
        spec = importlib.util.find_spec('xylem.wastewater_workbench_v5.controllers')
        self.assertIsNotNone(spec, 'v5 common decision controller is not implemented')
        from xylem.wastewater_workbench_v5 import engine, controllers
        from xylem.wastewater_workbench_v5.scenarios import configuration
        view = engine.public_state(engine.new_simulation(configuration(resource_enabled=False)))
        simple = controllers.recommend(view, 'simple')
        coordinated = controllers.recommend(view, 'coordinated')
        self.assertEqual(simple['rates'], coordinated['rates'])
        self.assertEqual(simple['transfers'], [])
        self.assertEqual(coordinated['transfers'], [])

    def test_controller_does_not_use_private_future_disturbances(self):
        spec = importlib.util.find_spec('xylem.wastewater_workbench_v5.controllers')
        self.assertIsNotNone(spec, 'v5 common decision controller is not implemented')
        from xylem.wastewater_workbench_v5 import engine, controllers
        from xylem.wastewater_workbench_v5.scenarios import configuration
        sim = engine.new_simulation(configuration())
        clean = engine.public_state(sim)
        poisoned = deepcopy(clean)
        poisoned['scenario'] = {'disturbances':[{'minute':5,'kind':'capacity','value':0}]}
        poisoned['future_recovery_minute'] = 9999
        a = controllers.recommend(clean, 'simple')
        b = controllers.recommend(poisoned, 'simple')
        self.assertEqual((a['rates'], a['transfers']), (b['rates'], b['transfers']))


class AttributionTests(unittest.TestCase):
    def test_resource_and_strategy_differences_are_not_conflated(self):
        spec=importlib.util.find_spec('xylem.wastewater_workbench_v5.comparison')
        self.assertIsNotNone(spec,'v5 attribution is not implemented')
        from xylem.wastewater_workbench_v5.comparison import attribute
        rows=[dict(case_id='x',variant='base',arm=arm,metrics={'uncontained_m3':value})
              for arm,value in [('A',100),('B',60),('C',100),('D',65)]]
        pairs=attribute(rows)
        self.assertEqual(pairs[0]['resource_effect_B_minus_A']['uncontained_m3'],-40)
        self.assertEqual(pairs[0]['strategy_effect_D_minus_B']['uncontained_m3'],5)
        self.assertEqual(pairs[0]['no_resource_strategy_C_minus_A']['uncontained_m3'],0)

    def test_unfinished_tank_and_transit_are_not_counted_as_handled(self):
        spec=importlib.util.find_spec('xylem.wastewater_workbench_v5.comparison')
        self.assertIsNotNone(spec,'v5 terminal accounting is not implemented')
        from xylem.wastewater_workbench_v5 import engine,disposition
        from xylem.wastewater_workbench_v5.comparison import terminal_metrics
        from xylem.wastewater_workbench_v5.scenarios import configuration
        cfg=configuration()
        cfg['disposition']['initial_inventory']={'A':20}
        s=engine.new_simulation(cfg)
        disposition.request(s,'store','B',10)
        m=terminal_metrics(s,[],[])
        self.assertEqual(m['terminal_storage_m3'],20)
        self.assertEqual(m['pending_transfer_m3'],10)
        self.assertEqual(m['returned_to_route_m3'],0)
        self.assertEqual(m['terminal_return_debt_m3'],20)


if __name__ == '__main__':
    unittest.main()
