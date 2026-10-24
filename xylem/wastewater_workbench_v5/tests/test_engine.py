"""Hand-calculated domain and conservation checks; no optimizer assertions."""
import importlib.util
import json
import unittest
from copy import deepcopy

from xylem.wastewater_workbench_v3 import engine as old

if importlib.util.find_spec('xylem.wastewater_workbench_v5.engine'):
    from xylem.wastewater_workbench_v5 import engine, disposition as d
else:
    engine = d = None


def fixture():
    profile = dict(mode='on_off', duty_rate=12, flow_levels=[0, 12],
        windows_minutes=[], min_on_minutes=0, min_off_minutes=0,
        min_setpoint_hold_minutes=0, max_starts_per_hour=20,
        ramp_up_m3_h_per_minute=None, ramp_down_m3_h_per_minute=None,
        basis='assumed', source='hand calculation fixture')
    firm = dict(id='A', name='A', route='R', stock=10., capacity=30.,
        production_rate=0., production_multiplier=1., production_enabled=True,
        rate=0., max_rate=12., ack_delay=0, switch_delay=0, min_hold=0,
        batch_ticks=1, last_switch=-4)
    confirmation = dict(known=True, allowed_types=['general'],
        confirmed_at_minutes=0, valid_until_minutes=10000)
    return dict(name='hand', step_minutes=15, substep_minutes=5,
        end_tick=192, production_end=192, auto_ack=True,
        firms={'A':firm}, routes={'R':dict(id='R', name='R', capacity=24.,
            buffer=30., stock=0., delay=0)}, capabilities={'A':profile},
        downstream_capacity=24., recovery=dict(maintenance_complete=True,
            temporary_supply_available=True, process_ready=True, effluent_verified=True,
            capacity_confirmation=dict(value=24.,confirmed_at_minutes=0,
                valid_until_minutes=10000,revision=0,basis='assumed',source='fixture')),
        notice_budget=dict(limit=20,window_minutes=1440),restart_guard=dict(enabled=False),
        disposition=dict(enabled=True, storage_capacity_m3=10., pump_rate_m3_h=12.,
            travel_minutes=10,ack_delay_minutes=0,setup_minutes=0,return_route='R',
            firm_types={'A':'general'}, acceptance={'storage':deepcopy(confirmation),
                'plant':deepcopy(confirmation)}))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(engine, 'v5 disposition engine is not implemented')

    def make(self, **updates):
        cfg=fixture();cfg['disposition'].update(updates)
        return engine.new_simulation(cfg)

    def test_unknown_and_incompatible_acceptance_blocks(self):
        s=self.make();d.confirm(s,'storage',False,60)
        self.assertFalse(d.assess(engine.public_state(s),'store','A',1)['allowed'])
        d.confirm(s,'storage',True,60,['different'])
        self.assertIn('incompatible_type',d.assess(engine.public_state(s),'store','A',1)['reasons'])

    def test_missing_water_type_never_inferred(self):
        s=self.make(firm_types={'A':None})
        self.assertIn('water_type_unknown',d.assess(engine.public_state(s),'store','A',1)['reasons'])

    def test_hand_calculation_departure_and_arrival(self):
        s=self.make();o=d.request(s,'store','A',3)
        engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['stock'],7)
        self.assertAlmostEqual(sum(x['volume'] for x in s['disposition']['storage_transit']),3)
        self.assertAlmostEqual(sum(s['disposition']['inventory'].values()),0)
        self.assertEqual(o['status'],'in_transit')
        engine.advance(s)
        self.assertAlmostEqual(s['disposition']['inventory']['A'],3)
        self.assertEqual(o['status'],'complete')
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)
        self.assertEqual(s['totals']['received'],0)

    def test_pending_and_transit_reserve_storage(self):
        s=self.make();d.request(s,'store','A',8)
        self.assertAlmostEqual(d.assess(engine.public_state(s),'store','A',3)['available'],2)
        engine.advance(s)
        self.assertAlmostEqual(d.assess(engine.public_state(s),'store','A',3)['available'],2)

    def test_expiry_stops_departures_but_arrivals_survive(self):
        s=self.make();d.confirm(s,'storage',True,5,['general'])
        o=d.request(s,'store','A',3);engine.advance(s,2)
        self.assertAlmostEqual(o['departed_m3'],1)
        self.assertAlmostEqual(s['disposition']['inventory']['A'],1)
        self.assertEqual(o['status'],'blocked')
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)

    def test_withdrawal_stops_new_not_existing_transit(self):
        s=self.make();o=d.request(s,'store','A',5);engine.advance(s)
        d.confirm(s,'storage',False,60);engine.advance(s,2)
        self.assertAlmostEqual(o['departed_m3'],3)
        self.assertAlmostEqual(s['disposition']['inventory']['A'],3)
        self.assertAlmostEqual(s['firms']['A']['stock'],7)

    def test_manual_ack_delay_reject_and_cancel(self):
        s=self.make(ack_delay_minutes=15);s['auto_ack']=False
        o=d.request(s,'store','A',5)
        with self.assertRaises(ValueError):d.feedback(s,o['id'],'ack')
        engine.advance(s);self.assertEqual(s['firms']['A']['stock'],10)
        d.feedback(s,o['id'],'reject')
        with self.assertRaises(ValueError):d.feedback(s,o['id'],'ack')
        o=d.request(s,'store','A',5);engine.advance(s);d.feedback(s,o['id'],'ack')
        engine.advance(s);d.feedback(s,o['id'],'cancel');engine.advance(s,2)
        self.assertAlmostEqual(o['departed_m3'],3)
        self.assertAlmostEqual(o['arrived_m3'],3)
        self.assertEqual(o['status'],'cancelled')
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)

    def test_common_notice_budget(self):
        cfg=fixture();cfg['notice_budget']['limit']=1;s=engine.new_simulation(cfg)
        d.request(s,'store','A',1)
        self.assertEqual(engine.public_state(s)['notice_budget']['remaining'],0)
        with self.assertRaises(ValueError):engine.publish(s,{'A':12})
        with self.assertRaises(ValueError):d.request(s,'store','A',1)

    def test_unknown_blocks_new_rate_but_observed_flow_remains(self):
        s=self.make();d.confirm(s,'plant',False,60)
        self.assertFalse(engine.assess_action(engine.public_state(s),{'A':12})['allowed'])
        cfg=fixture();cfg['firms']['A']['rate']=12;s=engine.new_simulation(cfg)
        d.confirm(s,'plant',False,60);engine.advance(s)
        self.assertGreater(s['firms']['A']['last_release'],0)

    def test_return_not_shortcut_to_received(self):
        s=self.make(initial_inventory={'A':5});o=d.request(s,'return','A',3)
        engine.advance(s)
        self.assertEqual(s['totals']['received'],0)
        self.assertAlmostEqual(s['disposition']['inventory']['A'],2)
        self.assertAlmostEqual(sum(x['volume'] for x in s['transit']),3)
        engine.advance(s,2)
        self.assertAlmostEqual(s['totals']['received'],3)
        self.assertAlmostEqual(o['arrived_m3'],3)
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)

    def test_shared_return_peak_with_direct_flow(self):
        cfg=fixture();cfg['downstream_capacity']=12;cfg['recovery']['capacity_confirmation']['value']=12
        cfg['firms']['A']['rate']=12;cfg['disposition']['initial_inventory']={'A':5}
        s=engine.new_simulation(cfg);o=d.request(s,'return','A',5);engine.advance(s)
        self.assertEqual(o['departed_m3'],0)
        self.assertEqual(s['disposition']['inventory']['A'],5)
        self.assertAlmostEqual(engine.mass_balance(s)['error'],0)

    def test_shared_storage_pump_not_double_used(self):
        s=self.make(initial_inventory={'A':3})
        a=d.request(s,'store','A',3);b=d.request(s,'return','A',3)
        engine.advance(s)
        self.assertLessEqual(a['departed_m3']+b['departed_m3'],3+1e-8)

    def test_route_buffer_reservation(self):
        cfg=fixture();cfg['routes']['R']['buffer']=2;cfg['disposition']['initial_inventory']={'A':5}
        s=engine.new_simulation(cfg)
        self.assertFalse(d.assess(engine.public_state(s),'return','A',3)['allowed'])
        d.request(s,'return','A',2)
        self.assertFalse(d.assess(engine.public_state(s),'return','A',1)['allowed'])

    def test_forecast_clone_retains_orders_and_mass(self):
        s=self.make();d.request(s,'store','A',5);engine.advance(s)
        view=json.loads(json.dumps(engine.public_state(s)))
        clone=engine.from_public(view)
        engine.advance(s,4);engine.advance(clone,4)
        self.assertEqual(s['disposition']['inventory'],clone['disposition']['inventory'])
        self.assertEqual(s['disposition']['orders'],clone['disposition']['orders'])
        self.assertAlmostEqual(engine.mass_balance(clone)['error'],0)

    def test_raw_json_recovery(self):
        s=self.make();d.request(s,'store','A',5);engine.advance(s)
        clone=json.loads(json.dumps(s));engine.advance(s,3);engine.advance(clone,3)
        self.assertEqual(s,clone)

    def test_disabled_parity_with_v3(self):
        cfg=fixture();cfg['disposition']['enabled']=False
        s=engine.new_simulation(cfg);legacy=old.new_simulation(cfg)
        engine.publish(s,{'A':12});old.publish(legacy,{'A':12})
        engine.advance(s,8);old.advance(legacy,8)
        for key in ('firms','routes','orders','totals','transit','history'):
            self.assertEqual(s[key],legacy[key],key)

    def test_disabled_resource_keeps_acceptance_gate_and_live_confirmation(self):
        cfg=fixture();cfg['disposition']['enabled']=False
        cfg['disposition']['firm_types']['A']=None
        s=engine.new_simulation(cfg)
        self.assertFalse(engine.assess_action(engine.public_state(s),{'A':12})['allowed'])
        engine.inject(s,'acceptance',destination='storage',known=False,valid_for_minutes=60)
        self.assertFalse(d.assess(engine.public_state(s),'store','A',1)['allowed'])

    def test_disabling_new_storage_does_not_discard_inventory_or_block_return(self):
        s=self.make(enabled=False,initial_inventory={'A':3})
        self.assertAlmostEqual(engine.mass_balance(s)['initial'],13)
        self.assertFalse(d.assess(engine.public_state(s),'store','A',1)['allowed'])
        d.request(s,'return','A',3);engine.advance(s,4)
        self.assertAlmostEqual(s['totals']['received'],3)

    def test_unconfigured_exact_legacy_public_parity(self):
        cfg=fixture();cfg.pop('disposition')
        s=engine.new_simulation(cfg);legacy=old.new_simulation(cfg)
        engine.advance(s,2);old.advance(legacy,2)
        self.assertEqual(engine.public_state(s),old.public_state(legacy))

    def test_forecast_strips_unowned_nested_information(self):
        s=self.make();d.request(s,'store','A',1)
        view=engine.public_state(s);view['disposition']['acceptance']['plant']['future_capacity']=100
        view['disposition']['orders'][0]['hidden_future_disturbances']=[100]
        view['disposition']['metrics']['future_actual']=100
        clone=engine.from_public(view)
        self.assertNotIn('future_capacity',clone['disposition']['acceptance']['plant'])
        self.assertNotIn('hidden_future_disturbances',clone['disposition']['orders'][0])
        self.assertNotIn('future_actual',clone['disposition']['metrics'])

    def test_confirmation_provenance_and_revision(self):
        s=self.make();first=engine.public_state(s)['disposition']['acceptance']['storage']
        self.assertEqual(first['basis'],'assumed')
        self.assertTrue(first['source'])
        d.confirm(s,'storage',True,60,['general'])
        self.assertGreater(s['disposition']['acceptance']['storage']['revision'],first['revision'])

    def test_invalid_parameters_are_rejected_without_mutation(self):
        s=self.make();before=json.dumps(s,sort_keys=True)
        for volume in (True,False,float('nan'),float('inf'),-1,0,'3'):
            with self.assertRaises(ValueError):d.request(s,'store','A',volume)
        with self.assertRaises(ValueError):d.request(s,'store','MISSING',1)
        for duration in (True,float('nan'),0,-1):
            with self.assertRaises(ValueError):d.confirm(s,'plant',True,duration,['general'])
        self.assertEqual(json.dumps(s,sort_keys=True),before)

    def test_delayed_new_rate_cannot_execute_after_acceptance_expiry(self):
        cfg=fixture();cfg['firms']['A']['ack_delay']=1
        cfg['disposition']['acceptance']['plant']['valid_until_minutes']=5
        s=engine.new_simulation(cfg);engine.publish(s,{'A':12});engine.advance(s,2)
        self.assertEqual(s['firms']['A']['rate'],0)
        self.assertEqual(s['orders'][0]['status'],'acknowledged')
        d.confirm(s,'plant',True,60,['general']);engine.advance(s)
        self.assertEqual(s['firms']['A']['rate'],12)

    def test_return_rechecks_plant_expiry_and_delayed_packets_keep_existing_route(self):
        s=self.make(initial_inventory={'A':3})
        d.confirm(s,'plant',True,5,['general']);o=d.request(s,'return','A',3)
        engine.advance(s,3)
        self.assertAlmostEqual(o['departed_m3'],1)
        self.assertAlmostEqual(s['totals']['received'],1)
        self.assertAlmostEqual(s['disposition']['inventory']['A'],2)

    def test_cancel_frees_only_undeparted_destination_reservation(self):
        s=self.make();o=d.request(s,'store','A',8);engine.advance(s)
        d.feedback(s,o['id'],'cancel')
        v=engine.public_state(s)
        self.assertAlmostEqual(v['disposition']['capacity_reserved_m3'],3)
        self.assertAlmostEqual(v['disposition']['available_storage_m3'],7)

    def test_restore_notice_reservation_cannot_be_spent_on_storage(self):
        cfg=fixture();cfg['firms']['A']['rate']=12
        cfg['notice_budget']['limit']=2;cfg['restart_guard']['enabled']=True
        s=engine.new_simulation(cfg);engine.publish(s,{'A':0})
        self.assertEqual(engine.public_state(s)['notice_budget']['available'],0)
        self.assertFalse(d.assess(engine.public_state(s),'store','A',1)['allowed'])
        engine.advance(s,2)
        self.assertEqual(len(s['orders']),2)
        self.assertTrue(s['restart_commitments'][0]['milestone_minutes'] is not None)

    def test_future_initial_acceptance_rejected(self):
        cfg=fixture();cfg['disposition']['acceptance']['storage']['confirmed_at_minutes']=5
        with self.assertRaises(ValueError):engine.new_simulation(cfg)

    def test_live_confirmation_event_does_not_leak_to_forecast(self):
        cfg=fixture();cfg['disposition']['acceptance']['storage']['known']=False
        cfg['disturbances']=[dict(minute=15,kind='acceptance',destination='storage',known=True,
            valid_for_minutes=60,allowed_types=['general'])]
        s=engine.new_simulation(cfg);clone=engine.from_public(engine.public_state(s))
        engine.advance(s,2);engine.advance(clone,2)
        self.assertTrue(s['disposition']['acceptance']['storage']['known'])
        self.assertFalse(clone['disposition']['acceptance']['storage']['known'])

    def test_maintenance_eta_is_public_but_not_a_capacity_guarantee(self):
        cfg=fixture();cfg['downstream_capacity']=0
        cfg['recovery']['capacity_confirmation']['value']=0
        cfg['recovery']['maintenance_complete']=False
        cfg['disposition']['initial_inventory']={'A':3}
        cfg['maintenance_plan']=dict(expected_restore_minutes=480,announced_at_minutes=0,
            revision=0,basis='assumed',source='synthetic initial maintenance estimate')
        s=engine.new_simulation(cfg);engine.advance(s,24)
        before=deepcopy(s['recovery']);balance=engine.mass_balance(s)
        engine.inject(s,'maintenance_notice',expected_restore_minutes=720,announced_at_minutes=360,
            revision=1,basis='assumed',source='synthetic maintenance delay report')
        self.assertEqual(engine.public_state(s)['maintenance_plan']['expected_restore_minutes'],720)
        self.assertEqual(s['recovery'],before)
        self.assertEqual(s['_physical_capacity'],0)
        self.assertEqual(engine.mass_balance(s),balance)
        self.assertEqual(s['disposition']['inventory']['A'],3)
        self.assertFalse(d.assess(engine.public_state(s),'return','A',1)['allowed'])
        self.assertEqual(s['events'][-1]['kind'],'maintenance_notice')

    def test_maintenance_notice_stale_future_and_invalid_rejected_atomically(self):
        cfg=fixture();cfg['maintenance_plan']=dict(expected_restore_minutes=480,
            announced_at_minutes=0,revision=0,basis='assumed',source='fixture')
        s=engine.new_simulation(cfg);engine.advance(s,2)
        engine.inject(s,'maintenance_notice',expected_restore_minutes=600,announced_at_minutes=15,
            revision=1,basis='assumed',source='delayed estimate')
        before=json.dumps(s,sort_keys=True)
        common=dict(expected_restore_minutes=720,announced_at_minutes=30,revision=2,basis='assumed',source='fixture')
        for change in ({'revision':1},{'announced_at_minutes':35},{'announced_at_minutes':10},
                       {'expected_restore_minutes':25},{'revision':True},{'revision':1.5},
                       {'expected_restore_minutes':float('nan')},{'source':''}):
            with self.assertRaises(ValueError):engine.inject(s,'maintenance_notice',**(common|change))
            self.assertEqual(json.dumps(s,sort_keys=True),before)

    def test_maintenance_plan_forecast_keeps_current_not_future_notice(self):
        cfg=fixture();cfg['maintenance_plan']=dict(expected_restore_minutes=480,
            announced_at_minutes=0,revision=0,basis='assumed',source='fixture')
        cfg['disturbances']=[dict(minute=15,kind='maintenance_notice',expected_restore_minutes=720,
            announced_at_minutes=15,revision=1,basis='assumed',source='future simulated report')]
        s=engine.new_simulation(cfg);view=engine.public_state(s)
        self.assertIn('maintenance_plan',view)
        view['maintenance_plan']['actual_future_recovery']=999
        clone=engine.from_public(view)
        self.assertNotIn('actual_future_recovery',clone['maintenance_plan'])
        engine.advance(s,2);engine.advance(clone,2)
        self.assertEqual(s['maintenance_plan']['revision'],1)
        self.assertEqual(clone['maintenance_plan']['revision'],0)

    def test_blocked_reason_remains_in_event_history_after_completion(self):
        s=self.make();d.confirm(s,'storage',True,5,['general'])
        o=d.request(s,'store','A',3);engine.advance(s,2)
        d.confirm(s,'storage',True,120,['general']);engine.advance(s,4)
        self.assertEqual(o['status'],'complete')
        blocked=[e for e in s['events'] if e['kind']=='disposition_blocked']
        self.assertTrue(any(e.get('order_id')==o['id'] and 'acceptance_expired' in e['reason'] for e in blocked))

    def test_local_dry_restart_rechecks_acceptance_in_both_resource_arms(self):
        for resource in (True,False):
            for condition in ('incompatible','unknown','expired'):
                with self.subTest(resource=resource,condition=condition):
                    cfg=fixture();cfg['firms']['A'].update(rate=12,stock=0,production_rate=0)
                    cfg['disposition']['enabled']=resource;s=engine.new_simulation(cfg)
                    engine.advance(s);self.assertFalse(s['firms']['A']['actuator']['running'])
                    if condition=='incompatible':d.confirm(s,'plant',True,120,['different'])
                    elif condition=='unknown':d.confirm(s,'plant',False,120)
                    else:
                        d.confirm(s,'plant',True,5,['general']);engine.advance(s)
                    s['firms']['A']['production_rate']=12
                    engine.advance(s)
                    self.assertEqual(s['firms']['A']['rate'],12,'existing target remains observable')
                    self.assertEqual(s['firms']['A']['last_release'],0)
                    self.assertFalse(s['firms']['A']['actuator']['running'])
                    self.assertAlmostEqual(s['firms']['A']['stock'],3)
                    self.assertAlmostEqual(engine.mass_balance(s)['error'],0)
                    d.confirm(s,'plant',True,120,['general']);engine.advance(s)
                    self.assertAlmostEqual(s['firms']['A']['last_release'],3)
                    self.assertTrue(s['firms']['A']['actuator']['running'])

    def test_running_observed_flow_not_stopped_by_new_incompatible_report(self):
        cfg=fixture();cfg['firms']['A'].update(rate=12,stock=10,production_rate=12)
        s=engine.new_simulation(cfg);d.confirm(s,'plant',True,120,['different'])
        engine.advance(s)
        self.assertAlmostEqual(s['firms']['A']['last_release'],3)
        self.assertTrue(s['firms']['A']['actuator']['running'])

    def test_unconfigured_legacy_dry_restart_parity(self):
        cfg=fixture();cfg.pop('disposition')
        cfg['firms']['A'].update(rate=12,stock=0,production_rate=0)
        s=engine.new_simulation(cfg);legacy=old.new_simulation(cfg)
        engine.advance(s);old.advance(legacy)
        s['firms']['A']['production_rate']=12;legacy['firms']['A']['production_rate']=12
        engine.advance(s);old.advance(legacy)
        self.assertEqual(engine.public_state(s),old.public_state(legacy))

    def test_unresolved_inventory_is_not_claimed_as_success(self):
        s=self.make(initial_inventory={'A':5});engine.advance(s,192)
        v=engine.public_state(s)
        self.assertGreaterEqual(v['disposition']['unresolved_m3'],15)
        self.assertEqual(s['totals']['received'],0)
        self.assertGreater(s['disposition']['metrics']['occupancy_m3_minutes'],0)


if __name__=='__main__':unittest.main()
