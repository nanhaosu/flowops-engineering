"""Human-facing derived facts, never future truth or invented performance."""
from ..wastewater_workbench_v3.capabilities import action_options
from ..wastewater_workbench_v3.recovery import receiving_status
from . import disposition

def build(view):
    d=view.get('disposition',{});firms=[]
    for f in view['firms']:
        firms.append(dict(id=f['id'],name=f['name'],water_type=d.get('firm_types',{}).get(f['id']),
            storage_reasons=disposition.acceptance_reasons(view,'storage',f['id']),
            plant_reasons=disposition.acceptance_reasons(view,'plant',f['id']),
            options=action_options(view,f['id']),stock=f['stock'],capacity=f['capacity'],rate=f['rate'],
            stored=d.get('inventory',{}).get(f['id'],0)))
    waiting=[o for o in d.get('orders',[]) if o['status'] in disposition.ACTIVE]
    blocked=[o for o in waiting if o.get('reasons')]
    acceptance={}
    for dest,record in d.get('acceptance',{}).items():
        status='unknown' if not record.get('known') else ('expired' if view['minute']>=record['valid_until_minutes'] else 'confirmed')
        acceptance[dest]=dict(record,status=status)
    return dict(firms=firms,receiving=receiving_status(view['recovery'],view['minute']),acceptance=acceptance,
        unresolved_m3=view['balance']['stored'],uncontained_m3=view['totals']['uncontained'],
        storage_return_debt_m3=sum(d.get('inventory',{}).values())+sum(p['volume'] for p in d.get('storage_transit',[]))+
            sum(p['volume'] for p in view['transit'] if p.get('disposition_order_id')),
        pending_orders=len(waiting),blocked_orders=len(blocked),
        limitations=['合成水量演练；所有设备数值和确认均为假设。','水类准入不是污染负荷验证；接收量不等于达标处理量。',
                      '转移单完成表示到达该去向；暂存仍需回送，尚未证明现场节水或人工效率收益。'])
