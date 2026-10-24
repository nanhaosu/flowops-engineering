"""Atomic local simulation sessions, exact-bundle preflight and public-only export."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import secrets
import threading
from uuid import uuid4

from . import engine, disposition, scenarios, controllers, presentation

HERE=Path(__file__).resolve().parent

class APIError(Exception):
    def __init__(self,code,message,status=400):
        super().__init__(message);self.code=code;self.message=str(message);self.status=status

def number(v,label,low=0,high=100000):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not low<=v<=high:
        raise APIError('invalid_input',label+' 必须是范围内的有限数字')
    return v

def boolean(v,label):
    if not isinstance(v,bool):raise APIError('invalid_input',label+' 必须为布尔值')
    return v

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

class Workbench:
    def __init__(self,session_path=None):
        self.path=Path(session_path) if session_path else HERE/'runtime/current.json'
        self.lock=threading.RLock();self.tokens={}
        if self.path.exists():
            self.data=read(self.path)
            if self.data.get('version')!=5:raise ValueError('Unsupported v5 session')
        else:
            self.data=dict(version=5,revision=0,session_id=str(uuid4()),simulation=engine.new_simulation(scenarios.configuration()),
                           context=dict(case_id='maintenance_delay',resource_enabled=True,variant='base'),branches=[])
            self._persist(self.data)

    def _persist(self,candidate):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid4().hex+'.pending')
        try:
            with temporary.open('x',encoding='utf-8') as stream:
                json.dump(candidate,stream,ensure_ascii=False,allow_nan=False,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        finally:
            if temporary.exists():temporary.unlink()

    def _state(self):
        view=engine.public_state(self.data['simulation'])
        return dict(version=5,revision=self.data['revision'],session_id=self.data['session_id'],simulation=view,
            context=deepcopy(self.data['context']),presentation=presentation.build(view),
            branches=[dict(id=b['id'],name=b['name'],minute=b['simulation']['minute'],case_id=b['context']['case_id']) for b in self.data['branches']])

    def state(self):
        with self.lock:return self._state()

    def catalog(self):return dict(cases=scenarios.catalog(),variants=list(scenarios.VARIANTS),synthetic=True)

    def evidence(self):
        path=HERE/'artifacts/comparison/results.json'
        if not path.is_file():return dict(status='not_run',message='对照结果尚未生成；不显示推测收益。')
        return dict(status='available',results=read(path),label='独立的48小时合成对照；不进入当前推荐')

    def export(self):
        with self.lock:return dict(label='FlowOps v5 公开观测；非真实设备指令',**self._state())

    def _bundle(self,payload):
        rates=payload.get('rates');transfers=payload.get('transfers')
        if not isinstance(rates,dict) or not isinstance(transfers,list) or len(transfers)>8:
            raise APIError('invalid_input','rates 为对象，transfers 为最多8条的数组')
        for fid,value in rates.items():
            if fid not in self.data['simulation']['firms']:raise APIError('invalid_input','未知企业')
            number(value,'rate')
        for t in transfers:
            if not isinstance(t,dict) or set(t)!={'direction','firm','volume'}:raise APIError('invalid_input','转移字段不完整')
            if t['direction'] not in ('store','return') or t['firm'] not in self.data['simulation']['firms']:
                raise APIError('invalid_input','未知转移方向或企业')
            if number(t['volume'],'volume')<=0:raise APIError('invalid_input','转移体积必须大于0')
        return deepcopy(dict(rates=rates,transfers=transfers))

    def _apply(self,sim,bundle):
        gate=engine.assess_action(engine.public_state(sim),bundle['rates'])
        if not gate['allowed']:raise ValueError(';'.join(gate['reasons']))
        engine.publish(sim,bundle['rates'])
        for action in bundle['transfers']:disposition.request(sim,**action)
        return gate

    def _archive(self,candidate,name):
        if not isinstance(name,str) or not name.strip() or len(name)>100:raise APIError('invalid_input','分支名称需1至100字')
        candidate['branches'].append(dict(id=str(uuid4()),name=name,simulation=deepcopy(candidate['simulation']),context=deepcopy(candidate['context'])))

    def action(self,payload):
        with self.lock:
            if not isinstance(payload,dict):raise APIError('invalid_input','Action must be an object')
            revision=payload.get('expected_revision')
            if isinstance(revision,bool) or not isinstance(revision,int):raise APIError('invalid_revision','需要整数版本号')
            if revision!=self.data['revision']:raise APIError('stale_revision','状态已改变，请刷新并重新预检',409)
            kind=payload.get('action')
            fields=dict(preview={'rates','transfers'},publish={'rates','transfers','preview_token'},recommend={'method'},
                step={'steps'},confirmation={'destination','known','valid_for_minutes','allowed_types'},feedback={'order_id','decision'},
                auto_ack={'enabled'},reset={'case_id','resource_enabled','variant'},fork={'name'},restore={'branch_id'})
            if not isinstance(kind,str) or kind not in fields or set(payload)-fields[kind]-{'action','expected_revision'}:
                raise APIError('invalid_action','未知操作或多余字段')
            try:
                if kind=='recommend':
                    result=controllers.recommend(engine.public_state(self.data['simulation']),payload.get('method','simple'))
                    return dict(self._state(),recommendation=result)
                if kind=='preview':
                    bundle=self._bundle(payload);trial=deepcopy(self.data['simulation']);reason=[];gate=None
                    try:gate=self._apply(trial,bundle)
                    except ValueError as exc:reason=str(exc).split(';')
                    token=secrets.token_urlsafe(24) if not reason and trial['tick']<trial['end_tick'] else None
                    if token:
                        self.tokens[token]=dict(revision=revision,bundle=bundle)
                        while len(self.tokens)>64:self.tokens.pop(next(iter(self.tokens)))
                    proposed=engine.public_state(trial) if not reason else None
                    return dict(self._state(),preview=dict(allowed=token is not None,reasons=reason,token=token,bundle=bundle,
                        assessment=gate,quota_after=proposed['notice_budget'] if proposed else None,
                        disposition_after=proposed.get('disposition') if proposed else None,
                        label='只验证当前输入下的发布条件；不保证未来没有积压或溢流'))
                candidate=deepcopy(self.data);sim=candidate['simulation']
                if kind=='publish':
                    bundle=self._bundle(payload);saved=self.tokens.get(payload.get('preview_token'))
                    if not saved or saved!={'revision':revision,'bundle':bundle}:raise APIError('invalid_preview','草稿或状态已变；请重新预检',409)
                    self._apply(sim,bundle)
                elif kind=='step':
                    steps=payload.get('steps')
                    if isinstance(steps,bool) or steps not in (1,4,16):raise APIError('invalid_input','步数需为1、4或16')
                    if sim['tick']>=sim['end_tick']:raise APIError('simulation_complete','48小时模拟已结束',422)
                    engine.advance(sim,steps)
                elif kind=='confirmation':
                    dest=payload.get('destination');known=boolean(payload.get('known'),'known')
                    if dest not in ('storage','plant'):raise APIError('invalid_input','未知接纳方')
                    duration=number(payload.get('valid_for_minutes'),'有效分钟',5,100000)
                    types=payload.get('allowed_types',sim['disposition']['acceptance'][dest].get('allowed_types'))
                    disposition.confirm(sim,dest,known,duration,types)
                elif kind=='feedback':
                    order_id=payload.get('order_id');decision=payload.get('decision')
                    if not isinstance(order_id,str):raise APIError('invalid_input','通知编号无效')
                    if order_id.startswith('D'):disposition.feedback(sim,order_id,decision)
                    else:engine.feedback(sim,order_id,decision)
                elif kind=='auto_ack':sim['auto_ack']=boolean(payload.get('enabled'),'enabled')
                elif kind=='fork':self._archive(candidate,payload.get('name'))
                elif kind=='reset':
                    resource=boolean(payload.get('resource_enabled'),'resource_enabled')
                    cfg=scenarios.configuration(payload.get('case_id'),resource,payload.get('variant','base'))
                    self._archive(candidate,'重置前 · '+str(sim['minute'])+'分钟')
                    candidate['simulation']=engine.new_simulation(cfg)
                    candidate['context']=dict(case_id=payload.get('case_id'),resource_enabled=resource,variant=payload.get('variant','base'))
                elif kind=='restore':
                    branch=next((b for b in candidate['branches'] if b['id']==payload.get('branch_id')),None)
                    if branch is None:raise APIError('invalid_input','找不到分支')
                    self._archive(candidate,'恢复前 · '+str(sim['minute'])+'分钟')
                    candidate['simulation']=deepcopy(branch['simulation']);candidate['context']=deepcopy(branch['context'])
                candidate['revision']+=1
                self._persist(candidate)
            except APIError:raise
            except (ValueError,TypeError,KeyError) as exc:raise APIError('action_rejected',str(exc),422) from exc
            except OSError as exc:raise APIError('save_failed','保存失败，未提交此操作，请重试',500) from exc
            self.data=candidate;self.tokens.clear()
            return self._state()
