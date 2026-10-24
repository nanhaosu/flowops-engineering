"""Behavioral checks for common, public-information candidate selection."""
from copy import deepcopy
import unittest

try:
    from xylem.wastewater_workbench_v3.planning import recommend, objective
except ImportError:
    recommend = objective = None


def configuration():
    from xylem.wastewater_workbench.scenario import scenario
    config = scenario()
    config['disturbances'] = []
    config['capabilities'] = {}
    for fid, firm in config['firms'].items():
        firm['ack_delay'] = 0
        firm['switch_delay'] = 0
        config['capabilities'][fid] = dict(
            mode='variable', duty_rate=None, flow_levels=[0, 10, 20, 30, 40],
            windows_minutes=[], min_on_minutes=10, min_off_minutes=10,
            min_setpoint_hold_minutes=60, max_starts_per_hour=2,
            ramp_up_m3_h_per_minute=2, ramp_down_m3_h_per_minute=2,
            basis='assumed', source='explicit synthetic planning test')
        firm['rate'] = 20
        firm['max_rate'] = 40
    config['recovery'] = dict(
        maintenance_complete=False, temporary_supply_available=True,
        process_ready=None, effluent_verified=None,
        capacity_confirmation=dict(value=65, confirmed_at_minutes=0,
            valid_until_minutes=2880, revision=1, basis='assumed',
            source='explicit synthetic planning test'))
    return config


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(recommend, 'v2 capability-aware planners must exist')
        from xylem.wastewater_workbench_v3.engine import new_simulation, public_state
        self.sim = new_simulation(configuration())
        self.view = public_state(self.sim)

    def test_assumed_profile_recommendation_never_certifies_field_execution(self):
        for method in ('simple', 'preview'):
            result = recommend(self.view, method)
            self.assertEqual(result['status'], 'conditional_simulation')
            self.assertFalse(result['executable_in_field'])
            self.assertEqual(result['production'], {})
            self.assertGreater(result['evaluated_candidates'], 0)

    def test_options_and_bundle_gate_apply_before_real_forecast(self):
        from xylem.wastewater_workbench_v3.capabilities import action_options, assess_bundle
        from xylem.wastewater_workbench_v3.engine import conditional_run
        self.view['capabilities']['A'].update(mode='on_off', duty_rate=20,
            flow_levels=[], ramp_up_m3_h_per_minute=None,
            ramp_down_m3_h_per_minute=None)
        for method in ('simple', 'preview'):
            result = recommend(self.view, method)
            for fid, target in result['rates'].items():
                self.assertIn(target, action_options(self.view, fid))
            self.assertTrue(assess_bundle(self.view, result['rates'])['allowed'])
            forecast = conditional_run(self.view, result['rates'], {}, result['horizon'])
            self.assertEqual(result['predicted_objective'],
                list(objective(self.view, forecast, result['rates'])))

    def test_pending_targets_are_excluded_and_existing_flow_is_retained(self):
        from xylem.wastewater_workbench_v3.engine import publish, public_state
        publish(self.sim, {'A': 0})
        view = public_state(self.sim)
        for method in ('simple', 'preview'):
            result = recommend(view, method)
            self.assertNotIn('A', result['rates'])
        self.assertEqual(self.sim['firms']['A']['rate'], 20)

    def test_single_notice_alternative_remains_available_before_choice(self):
        self.view['notice_budget'] = dict(limit=1, window_minutes=1440, used=0, remaining=1)
        for firm in self.view['firms']:
            firm['stock'] = 0
            firm['production_rate'] = 0
        self.view['firms'][0]['stock'] = 85
        self.view['firms'][0]['production_rate'] = 35
        self.view['balance']['initial'] = self.view['balance']['stored'] = 135
        for method in ('simple', 'preview'):
            result = recommend(self.view, method)
            changes = {fid: rate for fid, rate in result['rates'].items()
                       if rate != next(f['rate'] for f in self.view['firms'] if f['id'] == fid)}
            self.assertEqual(changes, {'A': 40})

    def test_exhausted_quota_returns_no_changes_instead_of_global_stop(self):
        self.view['notice_budget'] = dict(limit=0, window_minutes=1440, used=0, remaining=0)
        for method in ('simple', 'preview'):
            result = recommend(self.view, method)
            self.assertEqual(result['status'], 'conditional_simulation')
            current = {f['id']: f['rate'] for f in self.view['firms']}
            self.assertTrue(all(current[fid] == value for fid, value in result['rates'].items()))

    def test_unknown_capability_or_receiving_capacity_creates_no_new_actions(self):
        from xylem.wastewater_workbench_v3.engine import public_state
        for kind in ('mode', 'capacity', 'expiry'):
            view = public_state(self.sim)
            if kind == 'mode':
                view['capabilities']['A']['mode'] = 'unknown'
            elif kind == 'capacity':
                view['recovery']['capacity_confirmation'] = None
                view['downstream_capacity'] = None
            else:
                view['recovery']['capacity_confirmation']['valid_until_minutes'] = 60
            for method in ('simple', 'preview'):
                result = recommend(view, method)
                self.assertEqual(result['rates'], {})
                self.assertEqual(result['status'], 'needs_confirmation')
                self.assertIsNone(result['predicted_objective'])
                self.assertFalse(result['executable_in_field'])

    def test_unknown_observed_stock_or_actuator_state_defers_instead_of_guessing(self):
        for container, field in (('firm', 'stock'), ('firm', 'ack_delay'),
                                  ('actuator', 'physical_rate')):
            view = deepcopy(self.view)
            record = view['firms'][0]
            if container == 'actuator':
                record = record['actuator']
            record[field] = None
            for method in ('simple', 'preview'):
                result = recommend(view, method)
                self.assertEqual(result['rates'], {})
                self.assertEqual(result['status'], 'needs_confirmation')
                self.assertIsNone(result['predicted_objective'])

    def test_all_forecast_candidates_are_admissible_and_preview_covers_simple(self):
        from unittest.mock import patch
        from xylem.wastewater_workbench_v3.capabilities import action_options, assess_bundle
        from xylem.wastewater_workbench_v3.engine import conditional_run
        observed = {}
        for method in ('simple', 'preview'):
            observed[method] = set()

            def checked_forecast(view, rates=None, production=None, horizon=16):
                if rates is not None:
                    self.assertTrue(assess_bundle(view, rates)['allowed'])
                    for fid, value in rates.items():
                        self.assertIn(value, action_options(view, fid))
                    observed[method].add(tuple(sorted(rates.items())))
                return conditional_run(view, rates, production, horizon)

            with patch('xylem.wastewater_workbench_v3.planning.conditional_run', checked_forecast):
                recommend(self.view, method)
        self.assertTrue(observed['simple'])
        self.assertLessEqual(observed['simple'], observed['preview'])

    def test_preview_contains_simple_with_identical_horizon_and_objective(self):
        simple = recommend(self.view, 'simple')
        preview = recommend(self.view, 'preview')
        self.assertEqual(simple['horizon'], preview['horizon'])
        self.assertLessEqual(tuple(preview['predicted_objective']), tuple(simple['predicted_objective']))
        self.assertGreaterEqual(preview['evaluated_candidates'], simple['evaluated_candidates'])
        self.assertTrue(preview['includes_simple'])

    def test_nested_private_future_poisoning_and_input_mutation_are_blocked(self):
        poisoned = deepcopy(self.view)
        poison = dict(disturbances=[dict(tick=1, kind='capacity', value=0)],
                      physical_capacity=0, future_capacity=0)
        poisoned.update(poison)
        poisoned['scenario'] = poison
        poisoned['firms'][0].update(poison)
        poisoned['firms'][0]['actuator'].update(poison)
        poisoned['routes'][0].update(poison)
        poisoned['capabilities']['A'].update(poison)
        poisoned['recovery'].update(poison)
        poisoned['recovery']['capacity_confirmation'].update(poison)
        before = deepcopy(poisoned)
        for method in ('simple', 'preview'):
            self.assertEqual(recommend(self.view, method), recommend(poisoned, method))
        self.assertEqual(before, poisoned)

    def test_unknown_method_and_terminal_snapshot_are_rejected(self):
        with self.assertRaises(ValueError):
            recommend(self.view, 'oracle')
        self.view['tick'] = self.view['end_tick']
        with self.assertRaises(ValueError):
            recommend(self.view)


if __name__ == '__main__':
    unittest.main()
