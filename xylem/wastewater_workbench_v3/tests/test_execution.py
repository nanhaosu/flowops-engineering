"""Behavioral contracts for capability-aware physical execution."""
import unittest
from copy import deepcopy
from xylem.wastewater_workbench.scenario import scenario
try:
    from xylem.wastewater_workbench_v3 import engine, capabilities, recovery
except ImportError:
    engine = capabilities = recovery = None


def profile(mode='on_off', duty=20):
    return dict(mode=mode, duty_rate=duty, flow_levels=[0,10,20,30,40],
        windows_minutes=[[0,2880]], min_on_minutes=10, min_off_minutes=10,
        min_setpoint_hold_minutes=60, max_starts_per_hour=2,
        ramp_up_m3_h_per_minute=2, ramp_down_m3_h_per_minute=2,
        basis='assumed', source='synthetic test fixture')


def config():
    c=scenario(); c['disturbances']=[]; c['end_tick']=192
    c['capabilities']={i:profile('variable') for i in c['firms']}
    c['recovery']=dict(maintenance_complete=False, temporary_supply_available=True,
        process_ready=None, effluent_verified=None, capacity_confirmation=dict(value=65,
        confirmed_at_minutes=0, valid_until_minutes=2880, revision=1,
        basis='assumed', source='synthetic test fixture'))
    for f in c['firms'].values():
        f.update(ack_delay=0,switch_delay=0,rate=0,last_switch=-4,production_rate=0)
    return c


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(engine, 'v2 execution modules must exist')

    def test_fixed_duty_has_only_off_and_true_duty(self):
        c=config();c['capabilities']['A']=profile();s=engine.new_simulation(c)
        self.assertEqual(capabilities.action_options(engine.public_state(s),'A'),[0,20])
        with self.assertRaises(ValueError):engine.publish(s,{'A':10})
        self.assertEqual(s['orders'],[])

    def test_unknown_fields_block_new_orders_and_forecasts_without_zero(self):
        c=config();c['capabilities']['A']['min_on_minutes']=None
        s=engine.new_simulation(c);v=engine.public_state(s)
        self.assertEqual(capabilities.action_options(v,'A'),[])
        self.assertFalse(capabilities.assess_bundle(v,{'A':20})['allowed'])
        self.assertEqual(engine.conditional_run(v)['status'],'unknown')
        self.assertIsNone(engine.conditional_run(v)['totals'])

    def test_ramp_counts_setpoints_separately_from_starts(self):
        s=engine.new_simulation(config());engine.publish(s,{'A':40});engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['last_release'],3.75) # integral of linear 0-to30 ramp over15min
        self.assertEqual(s['firms']['A']['actuator']['physical_rate'],30)
        self.assertEqual(len([e for e in s['events'] if e['kind']=='start' and e.get('firm')=='A']),1)
        engine.publish(s,{'A':20});engine.advance(s,3)
        self.assertEqual(s['firms']['A']['rate'],40) # hold until minute60
        engine.advance(s)
        self.assertEqual(s['firms']['A']['rate'],20)
        self.assertEqual(len([e for e in s['events'] if e['kind']=='start' and e.get('firm')=='A']),1)

    def test_fixed_duty_dry_stop_has_fractional_runtime_not_smoothed_rate(self):
        c=config();c['capabilities']['A']=profile();c['firms']['A']['stock']=1
        s=engine.new_simulation(c);engine.publish(s,{'A':20});engine.advance(s)
        a=s['firms']['A']['actuator'];self.assertAlmostEqual(a['runtime_minutes'],3)
        self.assertEqual(a['physical_rate'],0);self.assertFalse(a['running'])
        e=[e for e in s['events'] if e['kind']=='stop' and e.get('firm')=='A'][0]
        self.assertEqual(e['minute'],3);self.assertEqual(e['reason'],'dry_protection')
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)

    def test_window_closes_locally_without_changing_permission(self):
        c=config();p=profile('window');p['windows_minutes']=[[0,10],[30,100]]
        c['capabilities']['A']=p;s=engine.new_simulation(c)
        engine.publish(s,{'A':20});engine.advance(s)
        self.assertEqual(s['firms']['A']['rate'],20)
        self.assertAlmostEqual(s['firms']['A']['last_release'],20/6)
        self.assertFalse(s['firms']['A']['actuator']['running'])
        engine.advance(s,2);self.assertTrue(s['firms']['A']['actuator']['running'])

    def test_pending_bundle_rejects_atomically_and_late_ack_cannot_revive(self):
        s=engine.new_simulation(config());s['auto_ack']=False
        order=engine.publish(s,{'A':20})[0]
        with self.assertRaises(ValueError):engine.publish(s,{'B':20,'A':40})
        self.assertEqual(len(s['orders']),1)
        engine.advance(s);self.assertEqual(s['firms']['A']['rate'],0)
        engine.feedback(s,order['id'],'reject')
        with self.assertRaises(ValueError):engine.feedback(s,order['id'],'ack')

    def test_minimum_on_off_and_start_limit_delay_physical_actions(self):
        c=config();c['capabilities']['A']=profile();c['capabilities']['A']['min_setpoint_hold_minutes']=0
        c['capabilities']['A']['max_starts_per_hour']=1
        s=engine.new_simulation(c);engine.publish(s,{'A':20});engine.advance(s)
        engine.publish(s,{'A':0});engine.advance(s)
        engine.publish(s,{'A':20});engine.advance(s,2)
        self.assertFalse(s['firms']['A']['actuator']['running'])
        engine.advance(s);self.assertTrue(s['firms']['A']['actuator']['running'])

    def test_private_capacity_does_not_leak_or_change_confirmation(self):
        s=engine.new_simulation(config());before=engine.public_state(s)
        engine.inject(s,'capacity',value=1);after=engine.public_state(s)
        self.assertEqual(before,after)
        self.assertEqual(after['downstream_capacity'],65)
        engine.inject(s,'maintenance',maintenance_complete=True)
        self.assertIsNone(engine.public_state(s)['recovery']['effluent_verified'])

    def test_confirmation_expiry_half_open_and_monotonic(self):
        c=config();c['recovery']['capacity_confirmation']['valid_until_minutes']=15
        s=engine.new_simulation(c);v=engine.public_state(s)
        self.assertEqual(engine.conditional_run(v,horizon=1)['status'],'conditional_simulation')
        self.assertEqual(engine.conditional_run(v,horizon=2)['status'],'unknown')
        engine.advance(s);self.assertIsNone(engine.public_state(s)['downstream_capacity'])
        with self.assertRaises(ValueError):engine.inject(s,'capacity_confirmation',value=30,confirmed_at_minutes=20,valid_until_minutes=100,revision=2,basis='assumed',source='test')
        engine.inject(s,'capacity_confirmation',value=30,confirmed_at_minutes=15,valid_until_minutes=100,revision=2,basis='assumed',source='test')
        self.assertEqual(engine.public_state(s)['downstream_capacity'],30)
        with self.assertRaises(ValueError):engine.inject(s,'capacity_confirmation',value=65,confirmed_at_minutes=15,valid_until_minutes=100,revision=1,basis='assumed',source='test')

    def test_quota_counts_actual_notices_and_resets_by_day(self):
        c=config();c['notice_budget']={'limit':1,'window_minutes':1440};s=engine.new_simulation(c)
        engine.publish(s,{'A':20})
        with self.assertRaises(ValueError):engine.publish(s,{'B':20})
        self.assertEqual(engine.public_state(s)['notice_budget']['remaining'],0)
        engine.advance(s,96)
        self.assertEqual(engine.public_state(s)['notice_budget']['remaining'],1)

    def test_route_peak_incompatible_duty_rejected_instead_of_throttled(self):
        c=config();c['capabilities']['A']=profile();c['routes']['R1']['capacity']=15
        with self.assertRaises(ValueError):engine.new_simulation(c)

    def test_resolution_and_nonfinite_inputs_rejected(self):
        c=config();c['step_minutes']=10
        with self.assertRaises(ValueError):engine.new_simulation(c)
        c=config();c['capabilities']['A']['min_on_minutes']=7
        with self.assertRaises(ValueError):engine.new_simulation(c)
        s=engine.new_simulation(config())
        with self.assertRaises(ValueError):engine.publish(s,{'A':float('nan')})
        self.assertEqual(s['orders'],[])

if __name__=='__main__':unittest.main()


class AdditionalExecutionTests(unittest.TestCase):
    def test_fractional_dry_stop_still_reserves_peak_in_same_cell(self):
        c=config();c['routes']['R1']['capacity']=30
        c['capabilities']['A']=profile();c['firms']['A'].update(stock=1,rate=20)
        c['firms']['B']['rate']=10
        # Existing requests physically fit30. A permission remains20 after dry-stop;
        # increase B only by direct pre-existing target to exercise local transition.
        s=engine.new_simulation(c);s['firms']['B']['rate']=20
        engine.advance(s)
        # B must wait for A's fixed20 peak rather than overlap at40 in first5min.
        bstart=[e for e in s['events'] if e['kind']=='start' and e.get('firm')=='B']
        self.assertTrue(any(e['reason']=='route_peak_interlock' and e.get('firm')=='B' for e in s['events']))
        self.assertTrue(all(e['minute']>=5 for e in bstart))

    def test_window_does_not_start_without_minimum_runtime_remaining(self):
        c=config();p=profile('window');p['windows_minutes']=[[0,5],[30,100]]
        c['capabilities']['A']=p;s=engine.new_simulation(c)
        engine.publish(s,{'A':20});engine.advance(s)
        self.assertEqual(s['firms']['A']['last_release'],0)
        self.assertFalse(s['firms']['A']['actuator']['running'])

    def test_event_stream_is_chronological_with_fractional_dry_stop(self):
        c=config();c['capabilities']['A']=profile();c['firms']['A']['stock']=1
        s=engine.new_simulation(c);engine.publish(s,{'A':20,'B':20});engine.advance(s)
        minutes=[e['minute'] for e in s['events']]
        self.assertEqual(minutes,sorted(minutes))

    def test_minimum_on_delays_off_command_but_safety_dry_stop_overrides(self):
        c=config();c['capabilities']['A']=profile();p=c['capabilities']['A']
        p['min_on_minutes']=30;p['min_setpoint_hold_minutes']=0
        s=engine.new_simulation(c);engine.publish(s,{'A':20});engine.advance(s)
        engine.publish(s,{'A':0});engine.advance(s)
        self.assertTrue(s['firms']['A']['actuator']['running'])
        self.assertAlmostEqual(s['firms']['A']['last_release'],5)
        engine.advance(s);self.assertFalse(s['firms']['A']['actuator']['running'])
        self.assertEqual([e['minute'] for e in s['events'] if e['kind']=='stop' and e.get('firm')=='A'],[30])

    def test_unknown_profile_keeps_existing_effective_flow(self):
        c=config();c['firms']['A']['rate']=20;c['capabilities']['A']['min_on_minutes']=None
        s=engine.new_simulation(c);engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['last_release'],5)
        self.assertEqual(s['firms']['A']['rate'],20)

    def test_forecast_cannot_read_future_world_and_preserves_transit(self):
        c=config();c['firms']['A']['rate']=20
        c['disturbances']=[dict(tick=1,kind='capacity',value=0)]
        s=engine.new_simulation(c);engine.advance(s)
        view=engine.public_state(s);a=engine.conditional_run(view,horizon=2)
        poisoned=deepcopy(view);poisoned['scenario']=c;poisoned['_physical_capacity']=0
        b=engine.conditional_run(poisoned,horizon=2)
        self.assertEqual(a,b);self.assertGreater(a['balance']['received'],0)
        self.assertAlmostEqual(a['balance']['error'],0)
        self.assertGreater(a['history'][0]['transit'],0)

class ObservationValidationTests(unittest.TestCase):
    def test_direct_forecast_unknown_inventory_or_runtime_has_no_numbers(self):
        view=engine.public_state(engine.new_simulation(config()))
        for section,key in [('firm','stock'),('actuator','physical_rate'),('route','capacity')]:
            changed=deepcopy(view)
            if section=='firm':changed['firms'][0][key]=None
            elif section=='actuator':changed['firms'][0]['actuator'][key]=None
            else:changed['routes'][0][key]=None
            result=engine.conditional_run(changed)
            self.assertEqual(result['status'],'unknown');self.assertIsNone(result['totals'])

    def test_invalid_public_capability_never_becomes_an_admissible_action(self):
        view=engine.public_state(engine.new_simulation(config()))
        for key,value in [('mode','unsupported'),('min_on_minutes',float('nan')),('max_starts_per_hour',0)]:
            changed=deepcopy(view);changed['capabilities']['A'][key]=value
            self.assertEqual(capabilities.action_options(changed,'A'),[])
            self.assertTrue(capabilities.profile_status(changed)['unknown_fields'])

    def test_forecast_ignores_nested_opaque_private_fields(self):
        view=engine.public_state(engine.new_simulation(config()))
        poisoned=deepcopy(view)
        poisoned['firms'][0]['future_capacity']=100
        poisoned['routes'][0]['private_schedule']=[0,999]
        poisoned['capabilities']['A']['future_mode']='teleport'
        poisoned['recovery']['physical_capacity']=0
        self.assertEqual(engine.conditional_run(view),engine.conditional_run(poisoned))

class PrefreezeRegressionTests(unittest.TestCase):
    def test_forecast_capacity_comes_from_validated_report_not_redundant_scalar(self):
        view=engine.public_state(engine.new_simulation(config()))
        for scalar in (0,None):
            changed=deepcopy(view);changed['downstream_capacity']=scalar
            result=engine.conditional_run(changed,horizon=1)
            self.assertEqual(result['status'],'conditional_simulation')
            self.assertAlmostEqual(result['totals']['received'],16.25)

    def test_missing_provenance_preserves_known_window_and_true_duty(self):
        c=config();c['capabilities']['A']=profile('window');c['capabilities']['A'].update(windows_minutes=[[0,10]],source=None)
        c['firms']['A']['rate']=20
        s=engine.new_simulation(c);engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['last_release'],10/3)
        self.assertFalse(s['firms']['A']['actuator']['running'])
        self.assertEqual([e['minute'] for e in s['events'] if e['kind']=='stop' and e.get('firm')=='A'],[10])
        self.assertEqual(engine.conditional_run(engine.public_state(s))['status'],'unknown')

    def test_partial_timing_preserves_window_but_does_not_invent_restart_permission(self):
        c=config();c['capabilities']['A']=profile('window')
        c['capabilities']['A'].update(windows_minutes=[[0,10],[30,100]],min_off_minutes=None)
        c['firms']['A']['rate']=20
        s=engine.new_simulation(c);engine.advance(s,3)
        self.assertAlmostEqual(s['firms']['A']['actuator']['runtime_minutes'],10)
        self.assertFalse(s['firms']['A']['actuator']['running'])
        self.assertTrue(any(e['reason']=='unknown_restart_protection' and e.get('firm')=='A' for e in s['events']))

    def test_unknown_ramp_holds_observed_flow_with_explicit_reason(self):
        c=config();c['firms']['A']['rate']=20;c['capabilities']['A']['ramp_up_m3_h_per_minute']=None
        s=engine.new_simulation(c);s['firms']['A']['actuator']['physical_rate']=10
        engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['last_release'],2.5)
        self.assertEqual(s['firms']['A']['actuator']['physical_rate'],10)
        self.assertTrue(any(e['reason']=='unknown_ramp_hold_observed_flow' and e.get('firm')=='A' for e in s['events']))

    def test_direct_forecast_requires_known_boolean_production_status(self):
        view=engine.public_state(engine.new_simulation(config()))
        for missing in (False,True):
            changed=deepcopy(view)
            if missing:del changed['firms'][0]['production_enabled']
            else:changed['firms'][0]['production_enabled']=None
            result=engine.conditional_run(changed,horizon=1)
            self.assertEqual(result['status'],'unknown');self.assertIsNone(result['totals'])


class FinalReviewRegressionTests(unittest.TestCase):
    def test_missing_provenance_cannot_allow_intermediate_initial_fixed_rate(self):
        for mode in ('on_off','window'):
            with self.subTest(mode=mode):
                c=config();c['capabilities']['A']=profile(mode,20)
                c['capabilities']['A']['source']=None;c['firms']['A']['rate']=10
                with self.assertRaises(ValueError):engine.new_simulation(c)

    def test_missing_provenance_keeps_valid_initial_off_or_duty_and_unknown_status(self):
        for mode in ('on_off','window'):
            for rate,release,runtime in ((0,0,0),(20,5,15)):
                with self.subTest(mode=mode,rate=rate):
                    c=config();c['capabilities']['A']=profile(mode,20)
                    c['capabilities']['A']['source']=None;c['firms']['A']['rate']=rate
                    s=engine.new_simulation(c);engine.advance(s)
                    self.assertEqual(s['firms']['A']['rate'],rate)
                    self.assertAlmostEqual(s['firms']['A']['last_release'],release)
                    self.assertAlmostEqual(s['firms']['A']['actuator']['runtime_minutes'],runtime)
                    self.assertEqual(s['firms']['A']['actuator']['physical_rate'],rate)
                    view=engine.public_state(s)
                    self.assertIsNone(view['capabilities']['A']['source'])
                    self.assertIn('source',capabilities.profile_status(view)['unknown_fields']['A'])
                    self.assertEqual(capabilities.action_options(view,'A'),[])
                    self.assertFalse(capabilities.assess_bundle(view,{'A':20 if rate==0 else 0})['allowed'])
                    forecast=engine.conditional_run(view,horizon=1)
                    self.assertEqual(forecast['status'],'unknown');self.assertIsNone(forecast['totals'])
                    self.assertAlmostEqual(engine.mass_balance(s)['error'],0)
