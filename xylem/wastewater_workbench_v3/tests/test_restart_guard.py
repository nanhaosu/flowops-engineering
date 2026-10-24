"""Shared restart promises exercise real orders and the five-minute engine."""
import unittest
from copy import deepcopy

from xylem.wastewater_workbench_v3 import engine, capabilities, planning
from xylem.wastewater_workbench_v3.tests.test_execution import config, profile


def guarded(limit=8):
    c = config()
    c['restart_guard'] = {'enabled': True}
    c['notice_budget'] = {'limit': limit, 'window_minutes': 1440}
    c['firms']['A'].update(rate=20, stock=55)
    return c


class RestartGuardTests(unittest.TestCase):
    def test_one_slot_rejects_without_mutation_two_slots_charge_restore(self):
        s = engine.new_simulation(guarded(1)); before = deepcopy(s)
        with self.assertRaisesRegex(ValueError, 'notice_budget'):
            engine.publish(s, {'A': 0})
        self.assertEqual(s, before)
        s = engine.new_simulation(guarded(2))
        orders = engine.publish(s, {'A': 0})
        self.assertEqual(len(orders), 1)
        self.assertEqual(engine.public_state(s)['notice_budget']['reserved'], 1)
        engine.advance(s)
        self.assertEqual(len(s['orders']), 2)
        restore = s['orders'][1]
        self.assertEqual((restore['created_minutes'], restore['status']), (0, 'acknowledged'))
        self.assertEqual(s['firms']['A']['rate'], 0)
        engine.advance(s, 4)
        ledger = s['restart_commitments'][0]
        self.assertEqual(ledger['target'], 20)
        self.assertEqual(ledger['status'], 'restored')
        self.assertEqual(s['firms']['A']['rate'], 20)
        self.assertEqual(engine.public_state(s)['notice_budget']['used'], 2)

    def test_multiple_closures_and_production_account_atomically(self):
        c = guarded(4); c['firms']['C']['rate'] = 20
        s = engine.new_simulation(c)
        gate = engine.assess_action(engine.public_state(s), {'A': 0, 'C': 0}, {'B': False})
        self.assertFalse(gate['allowed']); self.assertEqual(gate['required_notices'], 3)
        self.assertEqual(s['orders'], [])
        engine.publish(s, {'A': 0, 'C': 0})
        self.assertEqual(len(s['restart_commitments']), 2)
        with self.assertRaisesRegex(ValueError, 'notice_budget'):
            engine.publish(s, {}, {'B': False})

    def test_route_and_rate_lock_cannot_be_stolen_while_off(self):
        c = guarded(); c['routes']['R1']['capacity'] = 40
        s = engine.new_simulation(c); engine.publish(s, {'A': 0}); engine.advance(s)
        with self.assertRaisesRegex(ValueError, 'route_peak_capacity'):
            engine.publish(s, {'B': 40})
        with self.assertRaisesRegex(ValueError, 'restart_locked'):
            engine.publish(s, {'A': 10})
        self.assertEqual(len(s['orders']), 2)

    def test_hold_overlaps_ack_no_extra_serial_wait(self):
        c = guarded(); c['firms']['A'].update(ack_delay=2, switch_delay=1)
        s = engine.new_simulation(c)
        gate = engine.assess_action(engine.public_state(s), {'A': 0})
        self.assertTrue(gate['allowed'], gate)
        engine.publish(s, {'A': 0}); engine.advance(s, 9)
        ledger = s['restart_commitments'][0]
        self.assertEqual(ledger['off_effective_minutes'], 45)
        self.assertEqual(ledger['restore_dispatched_minutes'], 45)
        self.assertEqual(ledger['restore_effective_minutes'], 105)
        self.assertLessEqual(ledger['milestone_minutes'], 120)

    def test_proof_rejects_window_start_ramp_and_new_firm_spill(self):
        cases = []
        c = guarded(); c['capabilities']['A'].update(mode='window', windows_minutes=[[0, 30], [300, 1000]])
        cases.append(c)
        c = guarded(); c['capabilities']['A']['min_off_minutes'] = 300; cases.append(c)
        c = guarded(); c['capabilities']['A']['ramp_up_m3_h_per_minute'] = .05; cases.append(c)
        c = guarded(); c['firms']['A'].update(stock=89, production_rate=20); cases.append(c)
        for c in cases:
            with self.subTest(c=c):
                s = engine.new_simulation(c)
                self.assertFalse(engine.assess_action(engine.public_state(s), {'A': 0})['allowed'])
                self.assertEqual(s['orders'], [])

    def test_empty_is_ready_empty_never_actual_pumping(self):
        c = guarded(); c['firms']['A']['stock'] = 0
        s = engine.new_simulation(c); engine.publish(s, {'A': 0}); engine.advance(s, 6)
        ledger = s['restart_commitments'][0]
        self.assertEqual(ledger['status'], 'ready_empty')
        self.assertEqual(s['firms']['A']['actuator']['runtime_minutes'], 0)
        self.assertEqual(s['firms']['A']['actuator']['physical_rate'], 0)

    def test_unknown_expiry_and_same_bucket_dispatch_fit(self):
        c = guarded(); c['recovery']['capacity_confirmation'] = None
        s = engine.new_simulation(c)
        self.assertFalse(engine.assess_action(engine.public_state(s), {'A': 0})['allowed'])
        c = guarded(); c['recovery']['capacity_confirmation']['valid_until_minutes'] = 60
        self.assertFalse(engine.assess_action(engine.public_state(engine.new_simulation(c)), {'A': 0})['allowed'])
        c = guarded(); c['notice_budget']['window_minutes'] = 30; c['firms']['A']['ack_delay'] = 2
        self.assertFalse(engine.assess_action(engine.public_state(engine.new_simulation(c)), {'A': 0})['allowed'])

    def test_delayed_close_carries_token_and_original_deadline(self):
        c = guarded(2); c['notice_budget']['window_minutes'] = 120
        s = engine.new_simulation(c); engine.publish(s, {'A': 0})
        deadline = s['restart_commitments'][0]['deadline_minutes']
        engine.inject(s, 'response', firm='A', value=10)
        engine.advance(s, 8)
        self.assertEqual(engine.public_state(s)['notice_budget']['reserved'], 1)
        self.assertEqual(s['restart_commitments'][0]['deadline_minutes'], deadline)
        self.assertEqual(s['restart_commitments'][0]['status'], 'missed')
        engine.advance(s, 4)
        self.assertEqual(len(s['orders']), 2)
        self.assertEqual(s['orders'][1]['created_minutes'], 150)
        self.assertEqual(engine.public_state(s)['notice_budget']['used'], 1)
        self.assertTrue(any(e['kind'] == 'restart_missed' for e in s['events']))
        self.assertTrue(s['restart_commitments'][0]['route_reserved'])
        engine.advance(s, 12)
        ledger = s['restart_commitments'][0]
        self.assertEqual(ledger['status'], 'missed')
        self.assertFalse(ledger['route_reserved'])
        self.assertEqual(ledger['milestone_kind'], 'restored_late')
        self.assertGreater(ledger['milestone_minutes'], deadline)
        self.assertTrue(any(e['kind'] == 'restart_late_restored' for e in s['events']))

    def test_rejected_close_cancels_but_rejected_restore_fails_no_free_retry(self):
        s = engine.new_simulation(guarded()); order = engine.publish(s, {'A': 0})[0]
        engine.feedback(s, order['id'], 'reject')
        self.assertEqual(s['restart_commitments'][0]['status'], 'cancelled')
        self.assertEqual(engine.public_state(s)['notice_budget']['reserved'], 0)
        s = engine.new_simulation(guarded()); engine.publish(s, {'A': 0})
        s['auto_ack'] = False; engine.feedback(s, s['orders'][0]['id'], 'ack'); engine.advance(s)
        restart = s['orders'][1]; engine.feedback(s, restart['id'], 'reject')
        self.assertEqual(s['restart_commitments'][0]['status'], 'failed')
        engine.advance(s, 20); self.assertEqual(len(s['orders']), 2)
        with self.assertRaises(ValueError): engine.feedback(s, restart['id'], 'ack')

    def test_public_proof_and_forecast_replay_full_trajectory_without_leak(self):
        s = engine.new_simulation(guarded()); view = engine.public_state(s)
        poisoned = deepcopy(view); poisoned['scenario'] = {'disturbances': [{'minute': 0}]}
        poisoned['_physical_capacity'] = 0
        poisoned['restart_guard']['private_future'] = 'secret'
        self.assertEqual(engine.assess_action(view, {'A': 0}), engine.assess_action(poisoned, {'A': 0}))
        forecast = engine.conditional_run(view, {'A': 0})
        engine.publish(s, {'A': 0}); engine.advance(s, 16)
        self.assertEqual(forecast['history'], s['history'])
        self.assertEqual(forecast['notices'], 2)
        self.assertEqual(forecast['final_state']['restart_commitments'], engine.public_state(s)['restart_commitments'])
        self.assertNotIn('private_future', engine.from_public(poisoned)['restart_guard'])

    def test_confirmation_loss_is_breach_even_when_planner_needs_confirmation(self):
        s = engine.new_simulation(guarded()); engine.publish(s, {'A': 0})
        s['recovery']['capacity_confirmation'] = None
        self.assertEqual(planning.recommend(engine.public_state(s))['status'], 'needs_confirmation')
        engine.advance(s, 17)
        self.assertEqual(len(s['orders']), 2)
        self.assertNotIn(s['restart_commitments'][0]['status'], ('restored', 'ready_empty'))
        self.assertTrue(any(e['kind'] == 'restart_endangered' for e in s['events']))

    def test_guard_disabled_matches_inherited_engine_dynamics(self):
        from xylem.wastewater_workbench_v2 import engine as old
        c = config(); a = engine.new_simulation(c); b = old.new_simulation(c)
        for rates in ({'A': 20}, {'A': 0}, {'A': 40}):
            engine.publish(a, rates); old.publish(b, rates)
            engine.advance(a, 8); old.advance(b, 8)
        for key in ('history', 'orders', 'events', 'totals', 'firms', 'routes'):
            self.assertEqual(a[key], b[key])

    def test_manual_ack_is_not_invented_as_known_future_feedback(self):
        c = guarded(); c['auto_ack'] = False
        s = engine.new_simulation(c)
        gate = engine.assess_action(engine.public_state(s), {'A': 0})
        self.assertFalse(gate['allowed'])
        self.assertIn('restart_ack_timing_unconfirmed', gate['reasons'])
        self.assertEqual(s['orders'], [])

    def test_later_unknown_required_observation_marks_breach_without_false_restore(self):
        s = engine.new_simulation(guarded()); engine.publish(s, {'A': 0})
        # Unknown response timing after dispatch must not become a fresh promise.
        engine.advance(s)
        s['firms']['A']['ack_delay'] = None
        engine.advance(s, 16)
        self.assertEqual(s['restart_commitments'][0]['status'], 'missed')
        self.assertTrue(any(e['kind'] == 'restart_endangered' and e['reason'] == 'required_observation_unknown'
                            for e in s['events']))

    def test_unknown_dispatch_timing_fails_explicitly_keeps_unused_token(self):
        c = guarded(); c['firms']['A']['ack_delay'] = 1
        s = engine.new_simulation(c); engine.publish(s, {'A': 0})
        s['firms']['A']['ack_delay'] = None
        engine.advance(s, 2)
        self.assertEqual(s['restart_commitments'][0]['status'], 'failed')
        self.assertEqual(len(s['orders']), 1)
        self.assertEqual(engine.public_state(s)['notice_budget']['reserved'], 1)
        self.assertTrue(s['restart_commitments'][0]['route_reserved'])

    def test_transient_endangerment_retained_when_real_recovery_still_meets_deadline(self):
        s = engine.new_simulation(guarded()); engine.publish(s, {'A': 0})
        confirmation = deepcopy(s['recovery']['capacity_confirmation'])
        s['recovery']['capacity_confirmation'] = None; engine.advance(s)
        s['recovery']['capacity_confirmation'] = confirmation; engine.advance(s, 5)
        self.assertEqual(s['restart_commitments'][0]['status'], 'restored')
        self.assertTrue(any(e['kind'] == 'restart_endangered' for e in s['events']))
        self.assertTrue(any(e['kind'] == 'restart_restored' for e in s['events']))

    def test_later_production_spill_is_failure_even_when_physically_recovered(self):
        c = guarded(); c['firms']['A']['production_rate'] = 10
        s = engine.new_simulation(c); engine.publish(s, {'A': 0}); engine.advance(s)
        engine.inject(s, 'production', firm='A', value=100)
        engine.advance(s, 5)
        ledger = s['restart_commitments'][0]
        self.assertEqual(ledger['status'], 'failed')
        self.assertEqual(ledger['milestone_kind'], 'restored_after_breach')
        self.assertFalse(ledger['route_reserved'])
        self.assertTrue(any(e['kind'] == 'restart_recovered_after_breach' for e in s['events']))
        self.assertFalse(any(e['kind'] == 'restart_restored' for e in s['events']))

    def test_both_planners_use_the_same_full_engine_projection_and_lock(self):
        s = engine.new_simulation(guarded()); view = engine.public_state(s)
        for method in ('simple', 'preview'):
            action = planning.recommend(view, method)
            self.assertTrue(engine.assess_action(view, action['rates'])['allowed'])
            forecast = engine.conditional_run(view, action['rates'])
            self.assertEqual(action['predicted_objective'], list(planning.objective(view, forecast, action['rates'])))
        engine.publish(s, {'A': 0}); engine.advance(s)
        view = engine.public_state(s)
        for method in ('simple', 'preview'):
            self.assertNotIn('A', planning.recommend(view, method)['rates'])

    def test_explicit_cancel_requires_reason_releases_only_ledger_resources(self):
        s = engine.new_simulation(guarded(2)); engine.publish(s, {'A': 0})
        before = deepcopy(s['orders'])
        with self.assertRaises(ValueError): engine.cancel_restart(s, 'R1', '')
        engine.cancel_restart(s, 'R1', 'Operator explicitly abandons the restore promise')
        self.assertEqual(s['orders'], before)
        self.assertEqual(engine.public_state(s)['notice_budget']['reserved'], 0)
        self.assertFalse(s['restart_commitments'][0]['route_reserved'])
        self.assertEqual(s['restart_commitments'][0]['status'], 'cancelled')
        self.assertEqual(s['events'][-1]['kind'], 'restart_cancelled')
        c = guarded(2); c['routes']['R1']['capacity'] = 40
        s = engine.new_simulation(c); engine.publish(s, {'A': 0}); engine.advance(s)
        pending = deepcopy(s['orders'])
        engine.cancel_restart(s, 'R1', 'Cancel assurance, keep the ordinary restore order')
        self.assertEqual(s['orders'], pending)
        self.assertEqual(engine.public_state(s)['notice_budget']['used'], 2)
        self.assertFalse(capabilities.assess_bundle(engine.public_state(s), {'B': 40})['allowed'])
        engine.advance(s, 5)
        self.assertEqual(s['firms']['A']['rate'], 20)
        self.assertEqual(s['restart_commitments'][0]['status'], 'cancelled')

    def test_poisoned_nested_public_records_never_enter_forecast(self):
        s = engine.new_simulation(guarded()); engine.publish(s, {'A': 0}); view = engine.public_state(s)
        poisoned = deepcopy(view)
        poisoned['notice_budget']['private_future'] = 'secret'
        poisoned['totals']['private_future'] = 'secret'
        poisoned['orders'][0]['private_future'] = 'secret'
        poisoned['recovery']['capacity_confirmation']['private_future'] = 'secret'
        poisoned['restart_commitments'][0]['private_future'] = 'secret'
        poisoned['restart_guard']['private_future'] = 'secret'
        self.assertEqual(engine.conditional_run(view), engine.conditional_run(poisoned))

    def test_start_count_limit_and_captured_intermediate_target(self):
        c = guarded(); c['firms']['A']['rate'] = 10
        s = engine.new_simulation(c); engine.publish(s, {'A': 0}); engine.advance(s, 6)
        self.assertEqual(s['restart_commitments'][0]['target'], 10)
        self.assertEqual(s['orders'][1]['target'], 10)
        c = guarded(); c['capabilities']['A'] = profile('on_off')
        c['capabilities']['A']['max_starts_per_hour'] = 1
        c['notice_budget']['window_minutes'] = 55
        s = engine.new_simulation(c)
        s['capabilities']['A']['min_setpoint_hold_minutes'] = 0
        s['firms']['A']['actuator']['start_minutes'] = [0]
        self.assertFalse(engine.assess_action(engine.public_state(s), {'A': 0})['allowed'])

    def test_full_ramp_to_captured_target_is_required_even_when_approaching_from_above(self):
        c = guarded(); c['capabilities']['A'].update(min_setpoint_hold_minutes=0,
            min_on_minutes=0, min_off_minutes=0, ramp_down_m3_h_per_minute=.1)
        s = engine.new_simulation(c)
        s['firms']['A']['actuator']['physical_rate'] = 30
        gate = engine.assess_action(engine.public_state(s), {'A': 0})
        self.assertTrue(gate['allowed'])
        self.assertEqual(gate['restart_projections'][0]['projected_milestone_minutes'], 100)


if __name__ == '__main__': unittest.main()
