"""Public-engineering-inspired synthetic tasks, never a calibrated park dataset.

Public evidence supports maintenance, acceptance and coordination categories.
Every numerical profile below is a predeclared demonstration assumption.
"""
from copy import deepcopy
from ..wastewater_workbench_v3.scenarios import capabilities, original_cases

END_TICK = 192
VARIANTS = {
    'base': dict(storage=120, pump=20, ack=30, recovery_offset=0),
    'heldout_tight_slow': dict(storage=60, pump=12, ack=90, recovery_offset=120),
    'heldout_roomy_fast': dict(storage=180, pump=30, ack=15, recovery_offset=-120),
}


def catalog():
    return [
        dict(id='maintenance_delay', label='检修延期', description='接纳降容后持续产水，恢复较原预计晚；暂存必须回送。'),
        dict(id='confirmation_gap', label='接纳确认迟到与过期', description='存储接纳确认先迟到，后过期；不能用空余容量代替有效确认。'),
        dict(id='return_overlap', label='回送与后续产水叠加', description='接纳恢复后新来水增加，暂存回送共享通道和接纳能力。'),
        dict(id='unknown_compatibility', label='未知水类对照', description='企业水类未核实；有资源也不擅自允许转移。'),
    ]


def configuration(case_id='maintenance_delay', resource_enabled=True, variant='base'):
    if case_id not in {c['id'] for c in catalog()}:
        raise ValueError('unknown case_id')
    if variant not in VARIANTS:
        raise ValueError('unknown variant')
    if not isinstance(resource_enabled, bool):
        raise ValueError('resource_enabled must be boolean')
    cfg = deepcopy(original_cases()[0][0]['configuration'])
    p = VARIANTS[variant]
    restore = (900 if case_id == 'confirmation_gap' else 720) + p['recovery_offset']
    cfg.update(name=next(c['label'] for c in catalog() if c['id'] == case_id),
               end_tick=END_TICK, production_end=END_TICK, step_minutes=15,
               substep_minutes=5, auto_ack=True, capabilities=capabilities(),
               notice_budget=dict(limit=32, window_minutes=1440),
               restart_guard=dict(enabled=True))
    # Start with observable nonempty firm tanks; no resource arm gets less wastewater.
    for fid, stock in {'A':60, 'B':65, 'C':45, 'D':50}.items():
        cfg['firms'][fid]['stock'] = stock
    cfg['recovery'] = dict(maintenance_complete=False, temporary_supply_available=False,
        process_ready=None, effluent_verified=None,
        capacity_confirmation=dict(value=65, confirmed_at_minutes=0,
            valid_until_minutes=2880, revision=0, basis='assumed',
            source='synthetic receiving acceptance; not actual plant approval'))
    cfg['maintenance_plan'] = dict(expected_restore_minutes=480,announced_at_minutes=0,
        revision=0,basis='assumed',source='synthetic initial maintenance ETA; not receiving confirmation')
    events = [dict(minute=360,kind='maintenance_notice',expected_restore_minutes=restore,
        announced_at_minutes=360,revision=1,basis='assumed',
        source='synthetic announced repair delay; receiving confirmation remains separate')]
    for revision, minute, capacity in [(1,120,25),(2,restore,65)]:
        events += [dict(minute=minute,kind='capacity',value=capacity),
                   dict(minute=minute,kind='capacity_confirmation',value=capacity,
                        confirmed_at_minutes=minute,valid_until_minutes=2880,
                        revision=revision,basis='assumed',source='synthetic observed report')]
    events.append(dict(minute=restore,kind='maintenance',maintenance_complete=True))
    acceptance = dict(known=True,allowed_types=['pretreated_process'],
                      confirmed_at_minutes=0,valid_until_minutes=2880)
    if case_id == 'confirmation_gap':
        acceptance['known'] = False
        events.extend([
            dict(minute=360,kind='acceptance',destination='storage',known=True,
                 valid_for_minutes=300,allowed_types=['pretreated_process']),
            dict(minute=900,kind='acceptance',destination='storage',known=True,
                 valid_for_minutes=1980,allowed_types=['pretreated_process'])])
    if case_id == 'return_overlap':
        for fid in cfg['firms']:
            events.extend([dict(minute=restore+60,kind='production',firm=fid,value=1.5),
                           dict(minute=1440,kind='production',firm=fid,value=1.0)])
    firm_types = {fid:'pretreated_process' for fid in cfg['firms']}
    # A single storage compartment cannot receive the explicitly different class.
    firm_types['D'] = 'saline_stream'
    if case_id == 'unknown_compatibility':
        firm_types = {fid:None for fid in cfg['firms']}
    cfg['disposition'] = dict(enabled=resource_enabled,
        storage_capacity_m3=p['storage'],pump_rate_m3_h=p['pump'],travel_minutes=30,
        ack_delay_minutes=p['ack'],setup_minutes=15,return_route='R1',
        firm_types=firm_types,acceptance=dict(storage=acceptance,
            plant=dict(known=True,allowed_types=['pretreated_process','saline_stream'],
                       confirmed_at_minutes=0,valid_until_minutes=2880)),
        initial_inventory={})
    cfg['disturbances'] = sorted(events,key=lambda e:e['minute'])
    cfg['assumptions'] = [
        '合成工程演练；公开资料仅支持问题和约束类别，所有数值均非现场校准。',
        '四家持续产水48小时；同一混合设备画像、共同通知额度和恢复保障。',
        '水类标签仅代表假设接纳约定；不证明化学相容、去除效率或达标排放。',
        '仅一种固定暂存池和一台共享转移泵；新增资源不等于调度算法增益。',
        '未来扰动留在私有场景中；控制器只看当前公开观测和已知产水计划。',
        '接收量不是达标处理量；暂存、回送在途和剩余库存始终计入责任。',
    ]
    return cfg


def matrix_cases():
    rows = [dict(case_id=c['id'],variant=v,split='demonstration' if v=='base' else 'heldout_parameters')
            for c in catalog()[:3] for v in VARIANTS]
    rows.append(dict(case_id='unknown_compatibility',variant='base',split='control'))
    return rows
