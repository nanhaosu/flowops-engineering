"""Four dynamic tasks; every quantitative parameter is a declared assumption."""
from ..wastewater_workbench_v5 import scenarios as old

VARIANTS = ('base','slow_response','unknown_tail')


def catalog():
    return [dict(id='maintenance_delay',label='检修延期',description='协调排量、有限暂存与企业B批次延期；恢复仍需确认。'),
        dict(id='confirmation_gap',label='确认撤回',description='暂存和生产授权可能变化；旧指令不可在续期后自动复活。'),
        dict(id='return_overlap',label='回送与生产恢复叠加',description='暂停后恢复产水、启动清洗及回送共同占用容量。'),
        dict(id='unresolvable',label='声明动作范围内无法容纳',description='24小时零接纳约束下，未获生产调整权限的来水超过全部存储容量。')]


def configuration(case_id='maintenance_delay',resource_enabled=True,variant='base'):
    if case_id not in {c['id'] for c in catalog()} or variant not in VARIANTS:
        raise ValueError('unknown v6 task or profile')
    cfg=old.configuration('maintenance_delay' if case_id=='unresolvable' else case_id,resource_enabled,'base')
    cfg['name']=next(c['label'] for c in catalog() if c['id']==case_id)
    slow=variant=='slow_response'
    profile=dict(basis='assumed',source='v6 predeclared synthetic production profile; not field calibrated',
        owner='production_owner_B',authorized=True,authorized_until_minutes=2880,authority_revision=0,
        ack_minutes=90 if slow else 30,stop_lead_minutes=30,restart_lead_minutes=90 if slow else 45,
        batch_minutes=120,min_pause_minutes=60,residual_minutes=120 if slow else 60,
        residual_rate_m3_h=cfg['firms']['B']['production_rate']*.25,restart_flush_m3=4,
        planned_units_per_hour=100,restart_ready=False,restart_valid_until_minutes=2880,clearance_revision=0)
    if variant=='unknown_tail':
        profile['residual_minutes']=profile['residual_rate_m3_h']=None
    cfg['production_control']=dict(profiles={'B':profile})
    restore=max(e['minute'] for e in cfg['disturbances'] if e['kind']=='capacity')
    if case_id=='confirmation_gap':
        cfg['disturbances'].extend([
            dict(minute=240,kind='acceptance',destination='storage',known=False,valid_for_minutes=480),
            dict(minute=240,kind='production_confirmation',firm='B',record=dict(owner='production_owner_B',
                authorized=False,authorized_until_minutes=2880,authority_revision=1)),
            dict(minute=540,kind='production_confirmation',firm='B',record=dict(owner='production_owner_B',
                authorized=True,authorized_until_minutes=2880,authority_revision=2))])
    if case_id=='unresolvable':
        cfg['downstream_capacity']=0
        cfg['recovery']['capacity_confirmation']['value']=0
        cfg['disturbances']=[e for e in cfg['disturbances'] if e['kind'] not in ('capacity','capacity_confirmation','maintenance','maintenance_notice')]
        restore=1440
        cfg['maintenance_plan']['expected_restore_minutes']=restore
        cfg['receiving_ceiling']=dict(value=0,valid_until_minutes=1440,basis='assumed',
            source='declared no-receiving task constraint; not an ETA inference')
        cfg['production_floor']=dict(rates_m3_h={fid:cfg['firms'][fid]['production_rate'] for fid in ('A','C','D')},
            valid_until_minutes=1440,basis='assumed',source='declared non-adjustable production floor for bounded infeasibility task')
        cfg['disturbances'] += [dict(minute=restore,kind='capacity',value=65),
            dict(minute=restore,kind='capacity_confirmation',value=65,confirmed_at_minutes=restore,
                 valid_until_minutes=2880,revision=1,basis='assumed',source='synthetic capacity restoration'),
            dict(minute=restore,kind='maintenance',maintenance_complete=True)]
    cfg['disturbances'].append(dict(minute=restore,kind='production_confirmation',firm='B',record=dict(
        owner='production_owner_B',restart_ready=True,restart_valid_until_minutes=2880,clearance_revision=1)))
    cfg['disturbances'].sort(key=lambda e:e['minute'])
    cfg['assumptions'] += ['Only firm B may request production deferral under dated synthetic owner authority.',
        'Deferred manufacturing remains unmet output; tail and restart cleaning add physical water. No monetary or water-saving claim.']
    return cfg
