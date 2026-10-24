(function(root){
  const number=v=>v==null||!Number.isFinite(v)?'未知':v.toLocaleString('zh-CN',{minimumFractionDigits:1,maximumFractionDigits:1});
  const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function draftKey(d){return JSON.stringify({rates:Object.fromEntries(Object.entries(d.rates).sort()),transfers:d.transfers,production:Object.fromEntries(Object.entries(d.production||{}).sort())});}
  function productionMetrics(s){const p=s.production_control;return {interruptedMinutes:p?.metrics.interrupted_enterprise_minutes??null,unmetUnits:p?.metrics.unmet_production_units??null,tailWater:p?.tail_remaining_m3??null};}
  function canPublish(p,revision,d){return Boolean(p?.token&&p.revision===revision&&p.key===draftKey(d));}
  function transfer(direction,firm,raw){const volume=Number(raw);if(!String(raw).trim()||!Number.isFinite(volume)||volume<=0)throw Error('请输入大于0的有限体积');return {direction,firm,volume};}
  const status={notified:'等待反馈',acknowledged:'已确认 · 等待生效',transferring:'转移中',blocked:'受阻 · 尚未完成',in_transit:'在途',rejected:'已拒绝',cancelled:'已取消未出发部分'};
  function orderLabel(o){return o.status==='complete'?(o.direction==='store'?'已到达暂存 · 回送另记':'已到达管路 · 非达标处理'):status[o.status]||o.status;}
  function metrics(s,p){return {remaining:s.balance.stored,uncontained:s.totals.uncontained,received:s.totals.received,returnDebt:p.storage_return_debt_m3};}
  const reasons={water_type_unknown:'水类未知',acceptance_unknown:'接纳尚未确认',acceptance_expired:'接纳确认已过期',acceptance_future:'确认时间尚未到达',incompatible_type:'水类不在接纳范围',notice_budget:'通知额度不足',source_volume_unavailable:'源端可用水量不足',destination_capacity_reserved:'去向容量已占用或预留',storage_resource_disabled:'未启用额外暂存资源',disposition_disabled:'暂存功能未配置',no_receiving_capacity:'当前无已确认接纳能力',simulation_ended:'模拟已结束',route_headroom:'管路容量不足'};
  reasons.return_route_or_receiving_or_pump_capacity='回送等待管路、接纳或共享泵的剩余能力';
  reasons.local_start_acceptance='新启动尚未取得有效接纳许可';
  reasons.operator_cancelled_undeparted='已取消未出发部分，在途水继续保留';
  reasons.operator_rejected='协调人员拒绝了通知';
  const productionReasons={authority_unknown_or_denied:'生产授权未知或已撤回',authority_expired:'生产授权已过期',unknown_residual_minutes:'停后尾水时间未知',unknown_residual_rate_m3_h:'停后尾水流量未知',restart_clearance_unknown_or_denied:'恢复生产尚未确认',restart_clearance_expired:'恢复确认已过期',minimum_pause:'尚未满足最短暂停时间',residual_process_not_complete:'停后尾水过程尚未结束',restart_flush_space:'企业池不足以接纳恢复清洗水',receiving_unconfirmed_or_zero:'接纳能力未知或为零',plant_class_acceptance:'污水厂水类接纳前提不满足',opposite_pending_order:'存在相反的待执行生产指令',no_action_right:'该企业没有生产调整权限'};
  function reason(v){return String(v).split(';').map(x=>{const p=x.split(':');if(p[0]==='production')return `${p[1]} · ${productionReasons[p[2]]||p[2]}`;return (reasons[p[0]]||p[0])+(p.length>1?' · '+p.slice(1).join(' / '):'');}).join('；');}
  const variantName=v=>({base:'示范参数',slow_response:'较慢确认 / 较长尾水',unknown_tail:'尾水条件未知'}[v]||v);
  function productionFeedback(status){return status==='notified'?['ack','reject','cancel']:status==='acknowledged'?['cancel']:[];}
  const api={number,escape,draftKey,canPublish,transfer,orderLabel,metrics,productionMetrics,productionFeedback,reason,variantName};
  if(typeof module!=='undefined')module.exports=api;else root.FlowModel=api;
})(globalThis);
