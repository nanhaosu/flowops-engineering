const {test}=require('node:test');const assert=require('node:assert/strict');
let model;try{model=require('../static/model.js');}catch{}
test('remaining liability includes all contained water, not only emergency storage',()=>{
  assert.ok(model,'UI model required');
  assert.equal(model.metrics({balance:{stored:42},totals:{uncontained:3,received:19}}, {storage_return_debt_m3:7}).remaining,42);
});
test('unknown and zero retain different labels',()=>{
  assert.ok(model);assert.equal(model.number(null),'未知');assert.equal(model.number(0),'0.0');
});
test('completed storage order is arrival not treatment completion',()=>{
  assert.ok(model);assert.match(model.orderLabel({direction:'store',status:'complete'}),/到达暂存/);
  assert.match(model.orderLabel({direction:'return',status:'complete'}),/到达管路/);
});
test('changed draft invalidates publication even if token exists',()=>{
  assert.ok(model);const p={revision:3,key:model.draftKey({rates:{A:0},transfers:[]}),token:'ok'};
  assert.equal(model.canPublish(p,3,{rates:{A:1},transfers:[]}),false);
  assert.equal(model.canPublish(p,4,{rates:{A:0},transfers:[]}),false);
  assert.equal(model.canPublish(p,3,{rates:{A:0},transfers:[]}),true);
});
test('invalid numeric transfer is refused in UI rather than JSON null',()=>{
  assert.ok(model);assert.throws(()=>model.transfer('store','A',''));
  assert.throws(()=>model.transfer('store','A','Infinity'));
  assert.deepEqual(model.transfer('store','A','5'),{direction:'store',firm:'A',volume:5});
});
test('HTML in external labels is escaped',()=>{
  assert.ok(model);assert.equal(model.escape('<img onerror="x">'),'&lt;img onerror=&quot;x&quot;&gt;');
});
