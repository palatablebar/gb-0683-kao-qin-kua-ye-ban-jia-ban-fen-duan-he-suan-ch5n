from decimal import Decimal,ROUND_HALF_UP
from app.errors import DomainError
from app.modules.m2_mapping.coerce import SENTINELS,number

# 裁决：同一加班段不得同时领取工资与调休。已批准调休保留，重复发放的工资记待追回，不冲减调休余额。
PREMIUM=('OVERTIME','REST','HOLIDAY')
STATUSES=('已批准','待批')
CENT=Decimal('.01')
POLICY='APPROVED_FIRST'
POLICY_NOTE='同一加班段不得同时领取工资与调休；已批准调休保留，重复发放的工资记待追回，不冲减调休余额。'

def _blank(value):
    return str('' if value is None else value).strip().upper() in SENTINELS

def _decimal(value):
    return Decimal(0) if _blank(value) else Decimal(str(value))

def _minutes(text):
    hours=number(text)
    if hours<=0:raise DomainError('NUMBER','调休时长须为正数')
    minutes=hours*60
    if minutes!=minutes.to_integral_value():raise DomainError('NUMBER','调休时长须为整分钟')
    return int(minutes)

def _requests(rows):
    # 调休申请是业务记录，不随打卡版本失效；同班全部采信行按(时长,状态)去重，不一致则整班挡下。
    groups={}
    for r in rows:groups.setdefault((r['employee'],r['shift_day']),[]).append(r)
    out={}
    for key,members in groups.items():
        pairs={}
        for r in members:
            h,s=r.get('comp_hours',''),r.get('comp_status','')
            if _blank(h) and _blank(s):continue
            pairs.setdefault((str(h).strip(),str(s).strip()),[]).append(r['_line'])
        if pairs:out[key]=pairs
    return out

def _paid(shifts):
    # 工资核验以采信版本为准：只统计m3选中的IN/OUT行，旧版打卡不计入。
    out={}
    for shift in shifts:
        if shift['in'] is None or shift['out'] is None:continue
        key=(shift['employee'],shift['day'])
        out[key]=out.get(key,Decimal(0))+_decimal(shift['in'].get('paid'))+_decimal(shift['out'].get('paid'))
    return out

def comp_time(state):
    premium={}
    for c in state['calculations']:
        if c['code'] in PREMIUM:premium.setdefault((c['employee'],c['shift_day']),[]).append(c)
    paid=_paid(state.get('shifts',[]))
    requests=_requests(state['rows'])
    keys=sorted(set(premium)|set(paid)|set(requests))
    shifts={}
    for key in keys:
        shifts[key]={'employee':key[0],'shift_day':key[1],
            'accrued_minutes':sum(i['minutes'] for i in premium.get(key,[])),
            'granted_minutes':0,'pending_minutes':0,'paid':str(paid.get(key,Decimal(0))),
            'recovery_minutes':0,'recovery_amount':'0.00'}
    ledger=[];blocked=[];request_out=[];seq=0
    for key in keys:
        for item in sorted(premium.get(key,[]),key=lambda c:(c['start'],c['calendar_day'])):
            seq+=1
            ledger.append({'seq':seq,'employee':key[0],'shift_day':key[1],'kind':'ACCRUE','minutes':item['minutes'],
                'amount':'','calendar_day':item['calendar_day'],'start':item['start'],'end':item['end'],
                'code':item['code'],'note':'加班形成可调休时数'})
    for key in sorted(requests):
        pairs=requests[key];shift=shifts[key]
        if len(pairs)>1:
            lines=sorted(n for ls in pairs.values() for n in ls)
            for (hours,status),ls in sorted(pairs.items()):
                request_out.append({'employee':key[0],'shift_day':key[1],'hours':hours,'status':status,
                    'lines':sorted(ls),'minutes':0,'result':'BLOCKED','note':'同班申请不一致'})
            blocked.append({'employee':key[0],'shift_day':key[1],'code':'COMP_CONFLICT','minutes':0,
                'message':'同一班次存在多份不一致的调休申请，全部挡下','lines':lines})
            continue
        (hours,status),lines=next(iter(pairs.items()))
        req={'employee':key[0],'shift_day':key[1],'hours':hours,'status':status,'lines':sorted(lines),
            'minutes':0,'result':'','note':''}
        try:minutes=_minutes(hours)
        except DomainError:
            req.update(result='BLOCKED',note='调休时长无效、非正数或不是整分钟')
            blocked.append({'employee':key[0],'shift_day':key[1],'code':'COMP_HOURS','minutes':0,
                'message':'调休时长无效、非正数或不是整分钟','lines':sorted(lines)})
            request_out.append(req);continue
        req['minutes']=minutes
        if status not in STATUSES:
            req.update(result='BLOCKED',note='调休状态无效，须为已批准或待批')
            blocked.append({'employee':key[0],'shift_day':key[1],'code':'COMP_STATUS','minutes':minutes,
                'message':'调休状态无效，须为已批准或待批','lines':sorted(lines)})
            request_out.append(req);continue
        if status=='待批':
            req.update(result='PENDING',note='待审批，不计入调休余额')
            shift['pending_minutes']+=minutes;request_out.append(req);continue
        granted=min(minutes,shift['accrued_minutes']);excess=minutes-granted
        shift['granted_minutes']+=granted
        req.update(result='GRANTED',note='已批准，计入调休余额' if not excess else '已批准，超出可形成时数的部分被挡下')
        if excess:
            blocked.append({'employee':key[0],'shift_day':key[1],'code':'COMP_EXCEEDS','minutes':excess,
                'message':'申请超出该班可形成的调休时数，超出部分挡下','lines':sorted(lines)})
        request_out.append(req)
    for key in keys:
        shift=shifts[key];remaining=shift['granted_minutes']
        if not remaining:continue
        paid_total=Decimal(shift['paid']);recovered=Decimal(0)
        for item in sorted(premium.get(key,[]),key=lambda c:(c['start'],c['calendar_day'])):
            if remaining<=0:break
            covered=min(remaining,item['minutes']);remaining-=covered
            seq+=1
            ledger.append({'seq':seq,'employee':key[0],'shift_day':key[1],'kind':'GRANT','minutes':covered,
                'amount':'','calendar_day':item['calendar_day'],'start':item['start'],'end':item['end'],
                'code':item['code'],'note':'批准调休来自该加班段'})
            if paid_total>0:
                amount=(Decimal(str(item['rate']))*covered*Decimal(str(item['multiplier']))/60).quantize(CENT,rounding=ROUND_HALF_UP)
                if recovered+amount>paid_total:amount=paid_total-recovered
                recovered+=amount;seq+=1
                ledger.append({'seq':seq,'employee':key[0],'shift_day':key[1],'kind':'RECOVER','minutes':covered,
                    'amount':str(amount),'calendar_day':item['calendar_day'],'start':item['start'],'end':item['end'],
                    'code':item['code'],'note':'该加班段已领工资，重复部分待追回'})
        if paid_total>0:
            shift['recovery_minutes']=shift['granted_minutes'];shift['recovery_amount']=str(recovered.quantize(CENT))
    balances=[]
    for employee in sorted({k[0] for k in keys}):
        mine=[s for k,s in shifts.items() if k[0]==employee]
        balances.append({'employee':employee,
            'accrued_minutes':sum(s['accrued_minutes'] for s in mine),
            'granted_minutes':sum(s['granted_minutes'] for s in mine),
            'pending_minutes':sum(s['pending_minutes'] for s in mine),
            'balance_minutes':sum(s['granted_minutes'] for s in mine),
            'recovery_minutes':sum(s['recovery_minutes'] for s in mine),
            'recovery_amount':str(sum((Decimal(s['recovery_amount']) for s in mine),Decimal(0)).quantize(CENT))})
    totals={'accrued_minutes':sum(s['accrued_minutes'] for s in shifts.values()),
        'granted_minutes':sum(s['granted_minutes'] for s in shifts.values()),
        'pending_minutes':sum(s['pending_minutes'] for s in shifts.values()),
        'blocked_minutes':sum(b['minutes'] for b in blocked),
        'recovery_minutes':sum(s['recovery_minutes'] for s in shifts.values()),
        'recovery_amount':str(sum((Decimal(s['recovery_amount']) for s in shifts.values()),Decimal(0)).quantize(CENT)),
        'requests':len(request_out),
        'granted_requests':sum(1 for q in request_out if q['result']=='GRANTED'),
        'pending_requests':sum(1 for q in request_out if q['result']=='PENDING'),
        'blocked_requests':sum(1 for q in request_out if q['result']=='BLOCKED')}
    comp={'policy':POLICY,'policy_note':POLICY_NOTE,'requests':request_out,
        'shifts':[shifts[k] for k in keys],'ledger':ledger,'balances':balances,
        'blocked':blocked,'totals':totals}
    issues=verify(comp);comp['balanced']=not issues;comp['issues']=issues
    return comp

def verify(comp):
    # 调休对账不变量：任何重算后都必须满足，不满足则整批对账未平、暂缓归档。
    issues=[]
    if not comp:return issues
    shifts={(s['employee'],s['shift_day']):s for s in comp.get('shifts',[])}
    for key,s in shifts.items():
        granted=int(s['granted_minutes'])
        if not 0<=granted<=int(s['accrued_minutes']):issues.append('GRANT_RANGE:'+'/'.join(key))
        want=granted if Decimal(str(s['paid']))>0 else 0
        if int(s['recovery_minutes'])!=want:issues.append('RECOVER_MISMATCH:'+'/'.join(key))
        if not Decimal(0)<=Decimal(str(s['recovery_amount']))<=max(Decimal(str(s['paid'])),Decimal(0)):
            issues.append('RECOVER_RANGE:'+'/'.join(key))
    excess={}
    granted_req={}
    for q in comp.get('requests',[]):
        if q['result']=='GRANTED':
            k=(q['employee'],q['shift_day']);granted_req[k]=granted_req.get(k,0)+int(q['minutes'])
    for key,s in shifts.items():
        if int(s['granted_minutes'])>0 and granted_req.get(key,0)==0:
            issues.append('GRANT_WITHOUT_REQUEST:'+'/'.join(key))
    for b in comp.get('blocked',[]):
        if b['code']=='COMP_EXCEEDS':
            k=(b['employee'],b['shift_day']);excess[k]=excess.get(k,0)+int(b['minutes'])
    for q in comp.get('requests',[]):
        k=(q['employee'],q['shift_day']);s=shifts.get(k)
        if q['result']=='GRANTED':
            if not s or s['granted_minutes']+excess.get(k,0)!=int(q['minutes']):issues.append('REQUEST_LOST:'+'/'.join(k))
        elif q['result']=='PENDING':
            if not s or s['pending_minutes']<int(q['minutes']):issues.append('PENDING_LOST:'+'/'.join(k))
        elif q['result']=='BLOCKED':
            if not any(b['code'] in ('COMP_CONFLICT','COMP_HOURS','COMP_STATUS') and (b['employee'],b['shift_day'])==k for b in comp.get('blocked',[])):
                issues.append('BLOCK_LOST:'+'/'.join(k))
        else:issues.append('REQUEST_STATE:'+str(q.get('employee','?')))
    for b in comp.get('balances',[]):
        granted=sum(int(s['granted_minutes']) for s in comp['shifts'] if s['employee']==b['employee'])
        if int(b['balance_minutes'])!=granted:issues.append('BALANCE_MISMATCH:'+b['employee'])
    for kind,field in (('GRANT','granted_minutes'),('ACCRUE','accrued_minutes'),('RECOVER','recovery_minutes')):
        got=sum(int(e['minutes']) for e in comp.get('ledger',[]) if e['kind']==kind)
        want=sum(int(s[field]) for s in comp['shifts'])
        if got!=want:issues.append('LEDGER_'+kind)
    return issues
