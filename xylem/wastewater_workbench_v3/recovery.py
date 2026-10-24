"""Independent receiving-capacity reports and separate repair/process facts."""
from .capabilities import finite

STATUS_FIELDS=('maintenance_complete','temporary_supply_available','process_ready','effluent_verified')


def validate_confirmation(report,now_minutes,previous=None):
    if not isinstance(report,dict):raise ValueError('confirmation must be an object')
    for key in ('value','confirmed_at_minutes','valid_until_minutes','revision'):
        if not finite(report.get(key)):raise ValueError('invalid confirmation '+key)
    if report['value']<0 or report['confirmed_at_minutes']<0 or report['confirmed_at_minutes']>now_minutes:raise ValueError('future or invalid confirmation')
    if report['valid_until_minutes']<=report['confirmed_at_minutes']:raise ValueError('empty confirmation validity')
    if report['revision']<0 or int(report['revision'])!=report['revision']:raise ValueError('invalid confirmation revision')
    if report.get('basis') not in ('assumed','verified') or not report.get('source'):raise ValueError('confirmation provenance required')
    if previous and (report['revision']<=previous['revision'] or report['confirmed_at_minutes']<previous['confirmed_at_minutes']):raise ValueError('nonmonotonic confirmation')


def receiving_status(recovery,now_minutes):
    r=recovery.get('capacity_confirmation') if isinstance(recovery,dict) else None
    if not r:return dict(known=False,capacity_m3_h=None,reason='capacity_unconfirmed')
    try:validate_confirmation(r,now_minutes)
    except ValueError:return dict(known=False,capacity_m3_h=None,reason='capacity_invalid_or_future')
    if now_minutes>=r['valid_until_minutes']:return dict(known=False,capacity_m3_h=None,reason='capacity_confirmation_expired')
    return dict(known=True,capacity_m3_h=r['value'],reason='capacity_confirmed')
