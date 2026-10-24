"""Protocol, fairness, trace-integrity and independent metric checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xylem.wastewater_workbench_v3 import comparison, scenarios


class ProtocolTests(unittest.TestCase):
    def test_frozen_original_32_and_mixed_profiles(self):
        protocol = scenarios.build_protocol()
        self.assertEqual(len(protocol['cases']), 32)
        self.assertEqual(len(protocol['groups']), 8)
        self.assertEqual(len(protocol['configurations']), 256)
        self.assertEqual(len(protocol['groups']) * len(protocol['cases']) * len(protocol['methods']), 768)
        config = next(iter(protocol['configurations'].values()))
        self.assertEqual(config['end_tick'], 64)
        self.assertEqual(config['production_end'], 48)
        self.assertEqual(config['capabilities']['A']['mode'], 'on_off')
        self.assertEqual(config['capabilities']['A']['duty_rate'], 20)
        self.assertEqual(config['capabilities']['C']['windows_minutes'],
                         [[0, 360], [420, 1800], [1860, 2880]])
        self.assertEqual(config['capabilities']['D']['flow_levels'], [0, 10, 20, 30])
        self.assertTrue(all(p['basis'] == 'assumed' for p in config['capabilities'].values()))

    def test_confirmation_gap_is_independent_and_bounded(self):
        protocol = scenarios.build_protocol()
        config = protocol['configurations']['control__mixed_confirmation_gap/derate--ordinary']
        events = config['disturbances']
        restore = next(e for e in events if e['kind'] == 'capacity' and e['value'] == 65)
        confirm = next(e for e in events if e['kind'] == 'capacity_confirmation' and e['value'] == 65)
        self.assertEqual(confirm['tick'] - restore['tick'], 8)
        old = next(e for e in events if e['kind'] == 'capacity_confirmation' and e['value'] == 30)
        self.assertEqual(old['valid_until_minutes'], restore['tick'] * 15)
        completion = next(e for e in events if e['kind'] == 'maintenance'
                          and e.get('maintenance_complete') is True)
        self.assertEqual(completion['tick'] - restore['tick'], 16)
        self.assertIsNone(config['recovery']['process_ready'])
        self.assertIsNone(config['recovery']['effluent_verified'])

    def test_freeze_rejects_changed_protocol_and_completed_matrix(self):
        with tempfile.TemporaryDirectory(dir=comparison.HERE / 'artifacts') as directory:
            target = Path(directory)
            frozen = comparison.freeze_protocol(target / 'protocol.json')
            self.assertEqual(comparison.freeze_protocol(target / 'protocol.json'), frozen)
            damaged = deepcopy(frozen)
            damaged['protocol']['end_tick'] = 99
            (target / 'protocol.json').write_text(json.dumps(damaged), encoding='utf-8')
            with self.assertRaises(RuntimeError):
                comparison.freeze_protocol(target / 'protocol.json')
            (target / 'results.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                comparison.run_matrix(frozen, target)


class ExecutionComparisonTests(unittest.TestCase):
    def test_hold_full_event_chronology_and_independent_metrics(self):
        protocol = scenarios.build_protocol()
        config = protocol['configurations']['control__mixed_confirmation_gap/derate--ordinary']
        trace = comparison.run_controller(config, 'hold')
        self.assertEqual(len(trace['history']), 193)
        self.assertEqual(len(trace['decisions']), 48)
        inputs = {d['tick']: d['visible_input'] for d in trace['decisions']}
        self.assertEqual(inputs[60]['end_tick'], 64)
        self.assertEqual(inputs[64]['end_tick'], 192)
        self.assertEqual(inputs[92]['production_end'], 48)
        self.assertEqual(inputs[96]['production_end'], 144)
        self.assertEqual(trace['metrics'], comparison.derive_metrics(trace))
        self.assertLess(trace['metrics']['max_abs_balance_error'], 1e-6)
        self.assertEqual(trace['metrics']['prevented'], 0)
        self.assertGreater(trace['metrics']['physical_stops'], 0)
        self.assertEqual(trace['metrics']['setpoint_changes'], 0)
        self.assertTrue(all(not d['action']['executable_in_field'] for d in trace['decisions']))

    def test_audit_detects_metric_and_hidden_input_corruption(self):
        protocol = scenarios.build_protocol()
        config = protocol['configurations']['control__mixed_confirmed/none--easy']
        trace = comparison.run_controller(config, 'hold')
        comparison.audit_trace(trace, config)
        changed = deepcopy(trace)
        changed['metrics']['received'] += 1
        with self.assertRaises(AssertionError):
            comparison.audit_trace(changed, config)
        changed = deepcopy(trace)
        changed['decisions'][0]['visible_input']['disturbances'] = config['disturbances']
        changed['decisions'][0]['input_sha256'] = comparison.canonical_hash(
            changed['decisions'][0]['visible_input'])
        with self.assertRaises(AssertionError):
            comparison.audit_trace(changed, config)

    def test_unknown_inputs_counted_for_hold_and_replay_fairness_for_all_methods(self):
        protocol = scenarios.build_protocol()
        config = protocol['configurations']['control__mixed_confirmation_gap/derate--ordinary']
        traces = [comparison.run_controller(config, method) for method in ('hold', 'simple', 'preview')]
        self.assertEqual(len({t['decisions'][0]['input_sha256'] for t in traces}), 1)
        produced = [[h['generated'] for h in t['history']] for t in traces]
        self.assertEqual(produced[0], produced[1])
        self.assertEqual(produced[1], produced[2])
        for trace in traces:
            with self.subTest(method=trace['method']):
                self.assertEqual(trace['metrics']['receiving_unknown_decisions'], 4)
                self.assertGreaterEqual(trace['metrics']['unknown_decisions'], 4)
                self.assertEqual(comparison.audit_trace(trace, config, replay=True)['replayed_steps'], 192)

    def test_independent_event_accounting_detects_forged_cumulative_start_count(self):
        protocol = scenarios.build_protocol()
        config = protocol['configurations']['control__mixed_confirmed/none--easy']
        trace = comparison.run_controller(config, 'hold')
        trace['history'][-1]['starts']['A'] += 1
        trace['metrics'] = comparison.derive_metrics(trace)
        with self.assertRaises(AssertionError):
            comparison.audit_trace(trace, config)

    def test_intentionally_paused_matrix_resumes_only_same_hash(self):
        protocol = scenarios.build_protocol()
        # Explicit smoke subset is distinct from the full frozen matrix protocol.
        protocol['cases'] = protocol['cases'][:1]
        protocol['groups'] = protocol['groups'][:1]
        protocol['methods'] = ['hold']
        key = protocol['groups'][0]['id'] + '/' + protocol['cases'][0]['id']
        protocol['configurations'] = {key: protocol['configurations'][key]}
        with tempfile.TemporaryDirectory(dir=comparison.HERE / 'artifacts') as directory:
            frozen = comparison.freeze_protocol(Path(directory) / 'protocol.json', protocol=protocol)
            first = comparison.run_matrix(frozen, directory, max_new_runs=1)
            self.assertEqual(first['status'], 'paused')
            damaged = deepcopy(frozen)
            damaged['source_sha256']['comparison.py'] = '0' * 64
            with self.assertRaises(RuntimeError):
                comparison.run_matrix(damaged, directory, resume=True)
            # Model an intentional interrupt after atomic trace publication but
            # before its checkpoint committed. Adopt the complete trace without
            # re-running or overwriting it, only under the original freeze.
            state_path = Path(directory) / 'run_state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['completed'] = {}
            state_path.write_text(json.dumps(state), encoding='utf-8')
            with patch.object(comparison, 'run_controller', side_effect=AssertionError('must adopt saved trace')):
                final = comparison.run_matrix(frozen, directory, resume=True)
            self.assertEqual(len(final['rows']), 1)
            with self.assertRaises(FileExistsError):
                comparison.run_matrix(frozen, directory, resume=True)
            audit = comparison.audit(directory, replay=True)
            self.assertEqual(audit['traces'], 1)
            self.assertEqual(audit['replayed_steps'], 192)


if __name__ == '__main__':
    unittest.main()
