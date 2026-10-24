"""Matched-arm and independent restart-accounting regressions."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from xylem.wastewater_workbench_v3 import comparison, scenarios, engine
from xylem.wastewater_workbench_v3.tests.test_restart_guard import guarded
from xylem.wastewater_workbench_v3.tests.test_execution import profile


class ReviewRegressionTests(unittest.TestCase):
    def _decision(self, sim, rates):
        view = engine.public_state(sim)
        admission = comparison.admit_bundle(sim, rates)
        return dict(minute=view['minute'], visible_input=view, admission=admission,
                    action=dict(rates=rates, production={}))

    def _trace(self, sim, decisions):
        return dict(restart_guard=sim['restart_guard'], restart_commitments=sim['restart_commitments'],
                    orders=sim['orders'], events=sim['events'], decisions=decisions)

    def test_continuing_window_recovery_passes_real_engine_lifecycle_audit(self):
        c = guarded(); c['capabilities']['A'] = profile('window')
        c['capabilities']['A'].update(windows_minutes=[[0, 60]], min_on_minutes=60,
                                    min_setpoint_hold_minutes=0)
        sim = engine.new_simulation(c)
        sim['firms']['A']['actuator']['last_start_minutes'] = 0
        decision = self._decision(sim, {'A': 0})
        self.assertTrue(decision['admission']['admitted'])
        engine.advance(sim)
        trace = self._trace(sim, [decision])
        self.assertEqual(sim['restart_commitments'][0]['milestone_minutes'], 10)
        comparison.audit_restart_lifecycle(trace, c)
        self.assertTrue(sim['restart_commitments'][0]['milestone_evidence']['window_eligible'])
        # Empty/nonrunning readiness must still satisfy a new minimum-on window.
        c['firms']['A']['stock'] = 0
        sim = engine.new_simulation(c)
        sim['firms']['A']['actuator']['last_start_minutes'] = 0
        self.assertFalse(engine.assess_action(engine.public_state(sim), {'A': 0})['allowed'])

    def test_reserved_notice_budget_rejection_is_counted_as_guard_caused(self):
        c = guarded(1); sim = engine.new_simulation(c)
        decision = self._decision(sim, {'A': 0})
        self.assertEqual(decision['admission']['assessment']['reasons'], ['notice_budget'])
        trace = self._trace(sim, [decision])
        self.assertEqual(comparison.restart_metrics(trace)['restart_guard_rejected_decisions'], 1)
        self.assertIs(decision['admission']['guard_caused_refusal'], True)
        comparison.audit_restart_lifecycle(trace, c)

    def test_retained_route_reservation_rejection_is_guard_caused(self):
        c = guarded(); c['routes']['R1']['capacity'] = 40
        c['firms']['A']['ack_delay'] = 2
        sim = engine.new_simulation(c)
        first = self._decision(sim, {'A': 0}); engine.advance(sim, 3)
        restore_id = sim['restart_commitments'][0]['restore_order_id']
        engine.feedback(sim, restore_id, 'reject')
        second = self._decision(sim, {'B': 40})
        self.assertIn('route_peak_capacity:R1', second['admission']['assessment']['reasons'])
        trace = self._trace(sim, [first, second])
        self.assertEqual(comparison.restart_metrics(trace)['restart_guard_rejected_decisions'], 1)
        self.assertIs(second['admission']['guard_caused_refusal'], True)
        comparison.audit_restart_lifecycle(trace, c)

    def test_guard_independent_failure_and_disabled_arm_do_not_count(self):
        for enabled, limit in ((True, 0), (False, 0)):
            c = guarded(limit); c['restart_guard']['enabled'] = enabled
            sim = engine.new_simulation(c)
            decision = self._decision(sim, {'B': 20})
            self.assertFalse(decision['admission']['admitted'])
            self.assertIs(decision['admission'].get('guard_caused_refusal'), False)
            trace = self._trace(sim, [decision])
            self.assertEqual(comparison.restart_metrics(trace)['restart_guard_rejected_decisions'], 0)
            comparison.audit_restart_lifecycle(trace, c)

    def test_independent_audit_rejects_forged_causal_marker_even_with_rederived_metrics(self):
        for limit, rates, forged in ((1, {'A': 0}, False), (0, {'B': 20}, True)):
            c = guarded(limit); sim = engine.new_simulation(c)
            decision = self._decision(sim, rates)
            decision['admission']['guard_caused_refusal'] = forged
            trace = self._trace(sim, [decision])
            # Recomputed metrics alone must not legitimize a forged attribution.
            trace['metrics'] = comparison.restart_metrics(trace)
            with self.assertRaises(AssertionError): comparison.audit_restart_lifecycle(trace, c)


class RestartComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol = scenarios.build_protocol()
        cls.config = cls.protocol['configurations']['control__mixed_confirmed/none--easy']
        cls.trace = comparison.run_controller(cls.config, 'hold')

    def test_matched_configurations_differ_only_by_policy(self):
        p = self.protocol
        for original in scenarios.ORIGINAL_GROUPS:
            for case in p['cases']:
                left = deepcopy(p['configurations']['control__' + original['id'] + '/' + case['id']])
                right = deepcopy(p['configurations']['protected__' + original['id'] + '/' + case['id']])
                self.assertFalse(left.pop('restart_guard')['enabled'])
                self.assertTrue(right.pop('restart_guard')['enabled'])
                self.assertEqual(left, right)

    def test_lifecycle_metrics_separate_charged_orders_from_tokens(self):
        trace = deepcopy(self.trace)
        trace['restart_commitments'] = [dict(id='R1', firm='A', target=20,
            status='restored', reservation_active=False, route_reserved=False,
            restore_order_id='O2'), dict(id='R2', firm='B', target=10,
            status='missed', reservation_active=True, route_reserved=True,
            restore_order_id=None)]
        trace['events'].extend([dict(kind=kind, commitment_id=cid)
            for kind, cid in [('restart_reserved', 'R1'), ('restart_reserved', 'R2'),
                              ('restart_dispatched', 'R1'), ('restart_restored', 'R1'),
                              ('restart_missed', 'R2')]])
        trace['orders'] = [dict(id='O1', status='effective', created=0),
                           dict(id='O2', status='effective', created=1)]
        m = comparison.derive_metrics(trace)
        self.assertEqual(m['restart_reservations_created'], 2)
        self.assertEqual(m['restart_notices'], 1)
        self.assertEqual(m['restart_restored'], 1)
        self.assertEqual(m['restart_deadline_missed'], 1)
        self.assertEqual(m['restart_outstanding_reserved_slots'], 1)
        self.assertEqual(m['notices'], 2)

    def test_audit_rejects_fabricated_restart_success_even_with_rederived_metrics(self):
        trace = deepcopy(self.trace)
        trace['restart_commitments'] = [dict(id='R1', firm='A', target=20,
            status='restored', reservation_active=False, route_reserved=False,
            restore_order_id='missing', closing_order_id='missing', created_minutes=0,
            deadline_minutes=240, milestone_minutes=60, milestone_kind='restored')]
        trace['events'].append(dict(kind='restart_restored', commitment_id='R1',
                                    firm='A', minute=2880, tick=192))
        trace['metrics'] = comparison.derive_metrics(trace)
        with self.assertRaises(AssertionError):
            comparison.audit_trace(trace, self.config)

    def test_control_comparison_checks_all_384_legacy_metric_records(self):
        legacy = json.loads(comparison.LEGACY_RESULTS.read_text(encoding='utf-8'))
        rows = deepcopy(legacy['rows'])
        for row in rows:
            row['group'] = 'control__' + row['group']
        self.assertEqual(comparison.audit_control_compatibility(rows), 384)
        rows[-1]['preview']['runtime_minutes'] += 1
        with self.assertRaises(AssertionError):
            comparison.audit_control_compatibility(rows)

    def test_hold_trace_carries_policy_ledger_and_replays(self):
        self.assertIn('restart_commitments', self.trace)
        self.assertIn('restart_guard', self.trace)
        self.assertEqual(comparison.audit_trace(self.trace, self.config, replay=True)['replayed_steps'], 192)

    def test_real_closure_charges_restore_and_rejects_fabricated_physical_evidence(self):
        config = self.protocol['configurations']['protected__mixed_confirmed/none--easy']
        def close_once(view, method):
            return dict(rates={'A': 0} if view['tick'] == 0 else {}, production={},
                        executable_in_field=False, status='conditional_simulation')
        with patch('xylem.wastewater_workbench_v3.planning.recommend', side_effect=close_once):
            trace = comparison.run_controller(config, 'simple')
        self.assertEqual(trace['metrics']['restart_reservations_created'], 1)
        self.assertEqual(trace['metrics']['restart_notices'], 1)
        self.assertEqual(trace['metrics']['restart_restored'] + trace['metrics']['restart_ready_empty'], 1)
        self.assertEqual(trace['metrics']['notices'], 2)
        comparison.audit_trace(trace, config, replay=True)
        changed = deepcopy(trace)
        event = next(e for e in changed['events'] if e['kind'] == 'restart_restored')
        event['physical_rate'] = 0
        with self.assertRaises(AssertionError):
            comparison.audit_trace(changed, config)

    def test_complete_48h_both_arms_all_three_methods(self):
        traces = {}
        for arm in ('control', 'protected'):
            config = self.protocol['configurations'][arm + '__mixed_notice8/derate--ordinary']
            for method in scenarios.METHODS:
                trace = comparison.run_controller(config, method)
                self.assertEqual(comparison.audit_trace(trace, config, replay=True)['replayed_steps'], 192)
                traces[arm, method] = trace
        for method in scenarios.METHODS:
            a, b = traces['control', method], traces['protected', method]
            self.assertEqual(comparison._physical_initial(a['decisions'][0]['visible_input']),
                             comparison._physical_initial(b['decisions'][0]['visible_input']))
            self.assertEqual([h['generated'] for h in a['history']], [h['generated'] for h in b['history']])
        row = dict(group='control__mixed_notice8', case_id='derate--ordinary',
                   **{m: traces['control', m]['metrics'] for m in scenarios.METHODS})
        self.assertEqual(comparison.audit_control_compatibility([row], require_complete=False), 3)

    def _real_changed_world_trace(self, disturbance, window=1440):
        config = deepcopy(self.protocol['configurations']['protected__mixed_confirmed/none--easy'])
        config['notice_budget']['window_minutes'] = window
        config['disturbances'].append(disturbance)
        def close_once(view, method):
            return dict(rates={'A': 0} if view['tick'] == 0 else {}, production={},
                        executable_in_field=False, status='conditional_simulation')
        with patch('xylem.wastewater_workbench_v3.planning.recommend', side_effect=close_once):
            return comparison.run_controller(config, 'simple'), config

    def test_real_late_recovery_keeps_missed_count_and_audits_evidence(self):
        trace, config = self._real_changed_world_trace(
            dict(tick=0, kind='response', firm='A', value=10), window=120)
        comparison.audit_trace(trace, config, replay=True)
        m = trace['metrics']
        self.assertTrue('restart_late_restored' in m)
        self.assertEqual(m['restart_late_restored'], 1)
        self.assertEqual(m['restart_deadline_missed'], 1)
        self.assertEqual(m['restart_restored'] + m['restart_ready_empty'], 0)
        changed = deepcopy(trace)
        changed['restart_commitments'][0]['milestone_minutes'] = 1
        changed['metrics'] = comparison.derive_metrics(changed)
        with self.assertRaises(AssertionError):
            comparison.audit_trace(changed, config)

    def test_real_spill_then_recovery_is_failed_and_separate_from_met(self):
        trace, config = self._real_changed_world_trace(
            dict(tick=1, kind='production', firm='A', value=100))
        comparison.audit_trace(trace, config, replay=True)
        m = trace['metrics']
        self.assertTrue('restart_recovered_after_breach' in m)
        self.assertEqual(m['restart_recovered_after_breach'], 1)
        self.assertEqual(m['restart_failed'], 1)
        self.assertEqual(m['restart_restored'] + m['restart_ready_empty'], 0)
        changed = deepcopy(trace)
        changed['restart_commitments'][0]['status'] = 'restored'
        changed['metrics'] = comparison.derive_metrics(changed)
        with self.assertRaises(AssertionError):
            comparison.audit_trace(changed, config)

    def test_real_explicit_cancel_after_dispatch_preserves_charged_orders(self):
        config = self.protocol['configurations']['protected__mixed_confirmed/none--easy']
        sim = engine.new_simulation(config)
        view = engine.public_state(sim)
        admission = comparison.admit_bundle(sim, {'A': 0})
        engine.advance(sim, 3)
        self.assertIsNotNone(sim['restart_commitments'][0]['restore_order_id'])
        before = deepcopy(sim['orders'])
        engine.cancel_restart(sim, 'R1', 'Operator abandons this conditional promise')
        self.assertEqual(before, sim['orders'])
        trace = dict(restart_guard=sim['restart_guard'], restart_commitments=sim['restart_commitments'],
                     orders=sim['orders'], events=sim['events'], decisions=[dict(
                         minute=0, visible_input=view, admission=admission, action={})])
        comparison.audit_restart_lifecycle(trace, config)
        m = comparison.restart_metrics(trace)
        self.assertEqual(m['restart_cancelled'], 1)
        self.assertEqual(m['restart_notices'], 1)
        self.assertEqual(m['restart_outstanding_reserved_slots'], 0)
        changed = deepcopy(trace)
        next(e for e in changed['events'] if e['kind'] == 'restart_cancelled')['reason'] = ''
        with self.assertRaises(AssertionError):
            comparison.audit_restart_lifecycle(changed, config)


if __name__ == '__main__':
    unittest.main()
