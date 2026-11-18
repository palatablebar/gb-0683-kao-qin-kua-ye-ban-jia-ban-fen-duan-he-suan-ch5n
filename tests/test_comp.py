from copy import deepcopy
from decimal import Decimal
import pytest
from app.workflow import run,load
from app.modules.m5_calculate.comp import comp_time,verify
from app.modules.m6_reconcile.compare import balance
from app.modules.m7_archive.service import execute as archive
from app.modules.m7_archive.export_out import bundle

@pytest.fixture
def state():return run(load('dirty.csv'),{})

def shift(comp,employee):return next(s for s in comp['shifts'] if s['employee']==employee)
def balance_of(comp,employee):return next(b for b in comp['balances'] if b['employee']==employee)

def test_accrual_only_from_premium_segments(state):
    # 跨夜/周末/假日加班段才形成调休，REGULAR段不形成
    comp=state['comp']
    assert {e['code'] for e in comp['ledger'] if e['kind']=='ACCRUE'}=={'HOLIDAY'}
    assert comp['totals']['accrued_minutes']==18*120

def test_approved_comp_kept_and_duplicate_wage_recovered(state):
    # 裁决：保住已批准调休，重复工资记待追回，不冲减余额
    s=shift(state['comp'],'E001')
    assert s['granted_minutes']==120 and s['recovery_minutes']==120 and s['recovery_amount']=='120.00'
    assert balance_of(state['comp'],'E001')['balance_minutes']==120

def test_no_double_compensation(state):
    # 同一加班段不得同时领取工资和调休：有批准调休且已付工资的班次必须全额记追回
    for s in state['comp']['shifts']:
        if s['granted_minutes']>0 and Decimal(s['paid'])>0:
            assert s['recovery_minutes']==s['granted_minutes']
        if Decimal(s['paid'])==0:
            assert s['recovery_minutes']==0 and s['recovery_amount']=='0.00'

def test_recovery_never_exceeds_paid(state):
    for s in state['comp']['shifts']:
        assert Decimal(s['recovery_amount'])<=max(Decimal(s['paid']),Decimal(0))

def test_excess_request_blocked(state):
    s=shift(state['comp'],'E002');assert s['granted_minutes']==120
    b=next(b for b in state['comp']['blocked'] if b['employee']=='E002')
    assert b['code']=='COMP_EXCEEDS' and b['minutes']==180

def test_pending_request_not_in_balance(state):
    s=shift(state['comp'],'E003')
    assert s['pending_minutes']==90 and s['granted_minutes']==0 and s['recovery_minutes']==0

def test_invalid_hours_blocked(state):
    assert any(b['code']=='COMP_HOURS' and b['employee']=='E004' for b in state['comp']['blocked'])

def test_invalid_status_blocked(state):
    assert any(b['code']=='COMP_STATUS' and b['employee']=='E005' for b in state['comp']['blocked'])

def test_inconsistent_request_blocked(state):
    assert any(b['code']=='COMP_CONFLICT' and b['employee']=='E006' for b in state['comp']['blocked'])
    assert shift(state['comp'],'E006')['granted_minutes']==0

def test_original_requests_survive_recalculation(state):
    # 原始调休申请逐条保留，重算不丢记录（含被挡下的）
    again=run(load('dirty.csv'),{})
    key=lambda q:(q['employee'],q['shift_day'],q['hours'],q['status'],q['result'])
    assert [key(q) for q in again['comp']['requests']]==[key(q) for q in state['comp']['requests']]
    assert len(state['comp']['requests'])==7

def test_rerun_balances_identical(state):
    again=run(load('dirty.csv'),{})
    assert again['run_id']==state['run_id'] and again['comp']['balances']==state['comp']['balances']
    assert again['comp']['ledger']==state['comp']['ledger']

def test_version_change_recomputes_wages_and_comp_together():
    # 补卡核验结果变化（旧版->新版打卡）：工资与调休余额一起失效重算
    base=load('dirty.csv').decode('utf-8-sig')
    changed=base.replace('00001,E001,2026-10-01,1,IN,2026-10-01T22:00:00+08:00',
                         '00001,E001,2026-10-01,2,IN,2026-10-01T21:00:00+08:00',1)
    a,b=run(base.encode(),{}),run(changed.encode(),{})
    assert a['run_id']!=b['run_id'] and a['business']['total']!=b['business']['total']
    ea,eb=shift(a['comp'],'E001'),shift(b['comp'],'E001')
    assert eb['accrued_minutes']==ea['accrued_minutes']+60
    assert b['comp']['balanced'] and b['reconcile']['balanced']

def test_superseded_version_request_not_lost():
    # 旧版打卡上的调休申请不随版本淘汰丢失；与新版不一致时挡下待处理
    base=load('dirty.csv').decode('utf-8-sig')
    changed=base.replace('00002,E001,2026-10-01,1,OUT,2026-10-02T06:00:00+08:00,22:00,06:00,02:00,03:00,20,0,"夜班打卡\n车间一",,',
                         '00002,E001,2026-10-01,2,OUT,2026-10-02T06:00:00+08:00,22:00,06:00,02:00,03:00,20,0,"夜班打卡\n车间一",4,已批准',1)
    s=run(changed.encode(),{})
    reqs=[q for q in s['comp']['requests'] if q['employee']=='E001']
    assert {q['hours'] for q in reqs}=={'2','4'}
    assert any(b['code']=='COMP_CONFLICT' and b['employee']=='E001' for b in s['comp']['blocked'])

def test_comp_verify_clean(state):
    assert verify(state['comp'])==[] and state['comp']['balanced'] is True

def test_reconcile_includes_comp(state):
    r=state['reconcile']
    assert r['balanced'] is True and r['comp']['balanced'] is True
    assert r['comp']['granted_minutes']==240 and r['comp']['recovery_amount']=='240.00'

def test_tampered_balance_fails_reconcile(state):
    s=deepcopy(state);s['comp']['balances'][0]['balance_minutes']+=5
    assert balance(s)['balanced'] is False

def test_tampered_recovery_fails_reconcile(state):
    s=deepcopy(state);s['comp']['shifts'][0]['recovery_minutes']=0
    assert balance(s)['balanced'] is False

def test_dropped_request_fails_reconcile(state):
    s=deepcopy(state);s['comp']['requests']=[q for q in s['comp']['requests'] if q['result']!='GRANTED']
    assert balance(s)['balanced'] is False

def test_unbalanced_postpones_archive(state):
    s=deepcopy(state);s['comp']['balances'][0]['balance_minutes']+=7;s['reconcile']=balance(s)
    assert archive(s)['archive']['ready'] is False
    assert archive(deepcopy(state))['archive']['ready'] is True

def test_export_blocked_when_unbalanced(state,client):
    from app import store
    s=deepcopy(state);s['reconcile']['balanced']=False;s['run_id']='comp-unbalanced'
    store.save(s['run_id'],s)
    r=client.get('/api/archive/comp-unbalanced/export.csv')
    assert r.status_code==422 and r.get_json()['code']=='UNBALANCED'

def test_export_contains_comp_evidence(state):
    text=bundle(state)
    assert text.count('调休')>=len(state['comp']['ledger']) and '挡下' in text and 'COMP_EXCEEDS' in text

def test_comp_pages_render(client):
    assert '调休台账' in client.get('/m5_calculate').data.decode()
    assert '调休对账' in client.get('/m6_reconcile').data.decode()
    assert '被挡下的调休变更' in client.get('/m5_calculate').data.decode()

def test_comp_api_payload(client):
    r=client.post('/api/runs',json={'sample':'dirty.csv'});assert r.status_code==200
    comp=r.get_json()['comp']
    assert comp['policy']=='APPROVED_FIRST' and comp['totals']['granted_minutes']==240
