import importlib.util
import json
import unittest
from copy import deepcopy
from xylem.wastewater_workbench_v6 import engine, scenarios

if importlib.util.find_spec('xylem.wastewater_workbench_v6.tasks'):
    from xylem.wastewater_workbench_v6 import tasks
else:
    tasks=None


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tasks,'dynamic task advice and fair adapters are missing')

    def test_table_and_workbench_materialize_identical_action_rights(self):
        view=engine.public_state(engine.new_simulation(scenarios.configuration()))
        advice=tasks.recommend(view)
        a=tasks.materialize('status_table',view,advice)
        b=tasks.materialize('linked_workbench',view,advice)
        self.assertEqual(a,b)
        self.assertEqual(tasks.rights(view),tasks.rights(deepcopy(view)))
        advice['bundle']['production']['A']=False
        for mode in ('status_table','linked_workbench'):
            self.assertFalse(engine.assess_action(view,{},tasks.materialize(mode,view,advice)['production'])['allowed'])

    def test_no_hidden_future_event_can_change_shared_advice(self):
        view=engine.public_state(engine.new_simulation(scenarios.configuration()))
        poisoned=deepcopy(view)
        poisoned['scenario']={'disturbances':[{'minute':60,'kind':'capacity','value':0}]}
        poisoned['world_events']=[{'kind':'secret'}]
        self.assertEqual(tasks.recommend(view),tasks.recommend(poisoned))

    def test_unknown_tail_advice_does_not_create_pause(self):
        view=engine.public_state(engine.new_simulation(scenarios.configuration(variant='unknown_tail')))
        self.assertEqual(tasks.recommend(view)['bundle']['production'],{})

    def test_unknown_receiving_preserves_unknown_without_forecast(self):
        cfg=scenarios.configuration()
        cfg['recovery']['capacity_confirmation']=None
        advice=tasks.recommend(engine.public_state(engine.new_simulation(cfg)))
        self.assertEqual(advice['status'],'needs_confirmation')
        self.assertIsNone(advice['predicted_uncontained_m3'])
        self.assertEqual(advice['bundle'],dict(rates={},transfers=[],production={}))

    def test_independent_adapters_have_identical_execution(self):
        cfg=scenarios.configuration()
        a,b=engine.new_simulation(cfg),engine.new_simulation(cfg)
        for _ in range(12):
            av,bv=engine.public_state(a),engine.public_state(b)
            self.assertEqual(av,bv)
            advice=tasks.recommend(av)
            tasks.execute(a,tasks.materialize('status_table',av,advice))
            tasks.execute(b,tasks.materialize('linked_workbench',bv,advice))
            engine.advance(a,4);engine.advance(b,4)
        self.assertEqual(engine.public_state(a),engine.public_state(b))

    def test_unresolvable_task_has_declared_conservation_lower_bound(self):
        view=engine.public_state(engine.new_simulation(scenarios.configuration('unresolvable')))
        bound=tasks.deficit_bound(view)
        self.assertTrue(bound['known'])
        self.assertGreater(bound['lower_bound_uncontained_m3'],0)
        view.pop('production_floor')
        self.assertFalse(tasks.deficit_bound(view)['known'])

    def test_negative_metric_records_include_manufacturing_cost(self):
        reference=dict(uncontained_m3=20,unmet_production_units=0,notices=2)
        candidate=dict(uncontained_m3=10,unmet_production_units=100,notices=3)
        adverse=tasks.adverse(reference,candidate)
        self.assertEqual(adverse,dict(unmet_production_units=100,notices=1))

    def test_advice_materialization_never_mutates_public_input(self):
        view=engine.public_state(engine.new_simulation(scenarios.configuration()))
        before=json.dumps(view,sort_keys=True)
        advice=tasks.recommend(view)
        tasks.materialize('status_table',view,advice)
        self.assertEqual(before,json.dumps(view,sort_keys=True))


if __name__=='__main__':unittest.main()
