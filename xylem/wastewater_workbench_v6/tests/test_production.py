import importlib.util
import json
import unittest
from copy import deepcopy

from xylem.wastewater_workbench_v5.tests.test_engine import fixture
from xylem.wastewater_workbench_v5 import engine as previous

if importlib.util.find_spec('xylem.wastewater_workbench_v6.engine'):
    from xylem.wastewater_workbench_v6 import engine, production
else:
    engine = production = None


def configuration():
    cfg = fixture()
    cfg['firms']['A'].update(production_rate=12, stock=10, rate=0)
    cfg['production_control'] = dict(profiles={'A':dict(
        basis='assumed', source='hand-calculated demonstration',
        owner='production_owner_A', authorized=True, authorized_until_minutes=10000,
        authority_revision=0, ack_minutes=15, stop_lead_minutes=15,
        restart_lead_minutes=15, batch_minutes=60, min_pause_minutes=30,
        residual_minutes=30, residual_rate_m3_h=6, restart_flush_m3=2,
        planned_units_per_hour=12, restart_ready=True,
        restart_valid_until_minutes=10000, clearance_revision=0)})
    return cfg


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(engine, 'v6 production-aware executor is missing')

    def make(self):
        return engine.new_simulation(configuration())

    def pause(self):
        s = self.make()
        engine.publish(s, {}, {'A':False})
        engine.advance(s, 5)
        return s

    def test_ack_and_batch_boundary_are_not_immediate_shutdown(self):
        s = self.make()
        order = engine.publish(s, {}, {'A':False})[0]
        self.assertEqual(order['ack_due_minutes'], 15)
        self.assertEqual(order['earliest_minutes'], 60)
        engine.advance(s, 4)
        self.assertTrue(s['firms']['A']['production_enabled'])
        self.assertAlmostEqual(s['totals']['generated'], 12)
        engine.advance(s)
        self.assertFalse(s['firms']['A']['production_enabled'])
        self.assertEqual(order['effective_at_minutes'], 60)
        self.assertAlmostEqual(s['totals']['generated'], 13.5)

    def test_tail_existing_water_and_manufacturing_debt_are_preserved(self):
        s = self.pause()
        engine.advance(s)
        p = engine.public_state(s)['production_control']
        self.assertAlmostEqual(s['totals']['generated'], 15)
        self.assertAlmostEqual(s['firms']['A']['stock'], 25)
        self.assertAlmostEqual(p['metrics']['tail_generated_m3'], 3)
        self.assertAlmostEqual(p['metrics']['interrupted_enterprise_minutes'], 30)
        self.assertAlmostEqual(p['metrics']['unmet_production_units'], 6)
        self.assertAlmostEqual(p['metrics']['planned_units'], 18)
        self.assertAlmostEqual(p['metrics']['completed_units'], 12)
        self.assertAlmostEqual(engine.mass_balance(s)['error'], 0)

    def test_unknown_tail_denies_pause_without_zero_imputation(self):
        cfg = configuration()
        cfg['production_control']['profiles']['A']['residual_minutes'] = None
        s = engine.new_simulation(cfg)
        gate = engine.assess_action(engine.public_state(s), {}, {'A':False})
        self.assertFalse(gate['allowed'])
        self.assertIn('production:A:unknown_residual_minutes', gate['reasons'])
        with self.assertRaises(ValueError):
            engine.publish(s, {}, {'A':False})
        self.assertEqual(s['orders'], [])

    def test_unknown_authority_does_not_become_permission(self):
        cfg = configuration()
        cfg['production_control']['profiles']['A']['authorized'] = None
        s = engine.new_simulation(cfg)
        self.assertFalse(engine.assess_action(engine.public_state(s), {}, {'A':False})['allowed'])

    def test_expired_authority_after_ack_terminally_refuses_stale_order(self):
        cfg = configuration()
        cfg['production_control']['profiles']['A']['authorized_until_minutes'] = 45
        s = engine.new_simulation(cfg)
        order = engine.publish(s, {}, {'A':False})[0]
        engine.advance(s, 5)
        self.assertTrue(s['firms']['A']['production_enabled'])
        self.assertEqual(order['status'], 'rejected')
        self.assertIn('authority_expired', order['terminal_reason'])
        self.assertAlmostEqual(s['totals']['generated'], 15)

    def test_revoked_order_does_not_revive_after_renewal(self):
        s = self.make()
        order = engine.publish(s, {}, {'A':False})[0]
        engine.advance(s, 2)
        production.update(s, 'A', dict(owner='production_owner_A', authorized=False,
            authorized_until_minutes=500, authority_revision=1))
        engine.advance(s, 3)
        production.update(s, 'A', dict(owner='production_owner_A', authorized=True,
            authorized_until_minutes=500, authority_revision=2))
        engine.advance(s, 4)
        self.assertTrue(s['firms']['A']['production_enabled'])
        self.assertEqual(order['status'], 'rejected')

    def test_wrong_owner_and_stale_revision_cannot_confirm(self):
        s = self.make()
        before = deepcopy(s['production_control'])
        with self.assertRaises(ValueError):
            production.update(s, 'A', dict(owner='different', authorized=True,
                authorized_until_minutes=500, authority_revision=1))
        with self.assertRaises(ValueError):
            production.update(s, 'A', dict(owner='production_owner_A', authorized=True,
                authorized_until_minutes=500, authority_revision=0))
        self.assertEqual(s['production_control'], before)

    def test_revoke_and_renew_before_boundary_cannot_revive_pause(self):
        s = self.make()
        order = engine.publish(s, {}, {'A':False})[0]
        engine.advance(s, 2)
        production.update(s, 'A', dict(owner='production_owner_A', authorized=False,
            authorized_until_minutes=500, authority_revision=1))
        production.update(s, 'A', dict(owner='production_owner_A', authorized=True,
            authorized_until_minutes=500, authority_revision=2))
        engine.advance(s, 3)
        self.assertEqual(order['status'], 'rejected')
        self.assertTrue(s['firms']['A']['production_enabled'])

    def test_expired_then_renewed_before_boundary_cannot_revive_pause(self):
        cfg = configuration()
        cfg['production_control']['profiles']['A']['authorized_until_minutes'] = 30
        s = engine.new_simulation(cfg)
        order = engine.publish(s, {}, {'A':False})[0]
        engine.advance(s, 3)
        production.update(s, 'A', dict(owner='production_owner_A', authorized=True,
            authorized_until_minutes=500, authority_revision=1))
        engine.advance(s, 2)
        self.assertEqual(order['status'], 'rejected')
        self.assertTrue(s['firms']['A']['production_enabled'])

    def test_revoke_and_renew_clearance_cannot_revive_resume(self):
        s = self.pause()
        engine.advance(s)
        order = engine.publish(s, {}, {'A':True})[0]
        engine.advance(s)
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=False,
            restart_valid_until_minutes=500, clearance_revision=1))
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=True,
            restart_valid_until_minutes=500, clearance_revision=2))
        engine.advance(s, 2)
        self.assertEqual(order['status'], 'rejected')
        self.assertFalse(s['firms']['A']['production_enabled'])
        self.assertEqual(s['production_control']['metrics']['restart_flush_m3'], 0)

    def test_expired_clearance_renewal_requires_new_explicit_resume(self):
        s = self.pause()
        engine.advance(s)
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=True,
            restart_valid_until_minutes=105, clearance_revision=1))
        old = engine.publish(s, {}, {'A':True})[0]
        engine.advance(s)
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=True,
            restart_valid_until_minutes=500, clearance_revision=2))
        self.assertEqual(old['status'], 'rejected')
        new = engine.publish(s, {}, {'A':True})[0]
        engine.advance(s, 6)
        self.assertEqual(new['status'], 'effective')
        self.assertEqual(s['production_control']['metrics']['restart_count'], 1)

    def test_resume_requires_clearance_and_minimum_pause(self):
        s = self.pause()
        self.assertFalse(engine.assess_action(engine.public_state(s), {}, {'A':True})['allowed'])
        engine.advance(s)
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=False,
            restart_valid_until_minutes=500, clearance_revision=1))
        self.assertFalse(engine.assess_action(engine.public_state(s), {}, {'A':True})['allowed'])
        production.update(s, 'A', dict(owner='production_owner_A', restart_ready=True,
            restart_valid_until_minutes=500, clearance_revision=2))
        self.assertTrue(engine.assess_action(engine.public_state(s), {}, {'A':True})['allowed'])

    def test_restart_flush_once_and_unmet_output_is_not_erased(self):
        s = self.pause()
        engine.advance(s)
        order = engine.publish(s, {}, {'A':True})[0]
        engine.publish(s, {}, {'A':True})
        self.assertEqual(len(s['orders']), 2)
        engine.advance(s, 3)
        p = engine.public_state(s)['production_control']['metrics']
        self.assertEqual(order['effective_at_minutes'], 120)
        self.assertEqual(p['restart_count'], 1)
        self.assertAlmostEqual(p['restart_flush_m3'], 2)
        self.assertAlmostEqual(p['unmet_production_units'], 12)
        self.assertAlmostEqual(s['totals']['generated'], 20)
        self.assertAlmostEqual(engine.mass_balance(s)['error'], 0)

    def test_insufficient_tank_space_denies_restart_flush(self):
        s = self.pause()
        engine.advance(s)
        s['firms']['A']['stock'] = 29
        gate = engine.assess_action(engine.public_state(s), {}, {'A':True})
        self.assertIn('production:A:restart_flush_space', gate['reasons'])

    def test_terminal_tail_and_output_debt_remain_visible(self):
        s = self.pause()
        p = engine.public_state(s)['production_control']
        self.assertAlmostEqual(p['tail_remaining_m3'], 1.5)
        self.assertAlmostEqual(p['metrics']['unmet_production_units'], 3)

    def test_public_clone_retains_tail_debt_and_strips_private_events(self):
        s = self.pause()
        view = engine.public_state(s)
        view['world_events'] = [{'kind':'secret'}]
        view['production_control']['profiles']['A']['private_future'] = {'x':1}
        clone = engine.from_public(view)
        self.assertNotIn('private_future', json.dumps(engine.public_state(clone)))
        self.assertEqual(clone['scenario']['disturbances'], [])
        engine.advance(s)
        engine.advance(clone)
        self.assertEqual(s['production_control'], clone['production_control'])
        self.assertAlmostEqual(s['totals']['generated'], clone['totals']['generated'])

    def test_pump_stop_does_not_stop_wastewater_production(self):
        s = self.make()
        engine.advance(s, 4)
        self.assertEqual(s['firms']['A']['rate'], 0)
        self.assertAlmostEqual(s['totals']['generated'], 12)

    def test_cancel_pending_production_preserves_already_generated_water(self):
        s = self.make()
        order = engine.publish(s, {}, {'A':False})[0]
        engine.advance(s, 2)
        engine.feedback(s, order['id'], 'cancel')
        engine.advance(s, 3)
        self.assertEqual(order['status'], 'cancelled')
        self.assertTrue(s['firms']['A']['production_enabled'])
        self.assertAlmostEqual(s['totals']['generated'], 15)

    def test_unconfigured_execution_retains_v5_parity(self):
        cfg = configuration()
        cfg.pop('production_control')
        a, b = engine.new_simulation(cfg), previous.new_simulation(cfg)
        engine.advance(a, 12)
        previous.advance(b, 12)
        self.assertEqual(engine.public_state(a), previous.public_state(b))

    def test_initial_paused_without_observed_state_is_refused(self):
        cfg = configuration()
        cfg['firms']['A']['production_enabled'] = False
        with self.assertRaisesRegex(ValueError, 'initial paused'):
            engine.new_simulation(cfg)

    def test_unknown_manufacturing_rate_cannot_report_zero_cost(self):
        cfg = configuration()
        cfg['production_control']['profiles']['A']['planned_units_per_hour'] = None
        with self.assertRaisesRegex(ValueError, 'manufacturing rate'):
            engine.new_simulation(cfg)


if __name__ == '__main__':
    unittest.main()
