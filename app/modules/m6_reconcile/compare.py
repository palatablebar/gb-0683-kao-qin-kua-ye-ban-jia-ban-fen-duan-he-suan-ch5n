from app.modules.m5_calculate.comp import verify

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine'])
    accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    comp=state.get('comp') or {}
    issues=verify(comp)
    totals=comp.get('totals',{})
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'balanced':accounted==state['input_count'] and not issues,
        'business':state.get('business',{}),
        'comp':{'balanced':not issues,'issues':issues,
            'accrued_minutes':totals.get('accrued_minutes',0),
            'granted_minutes':totals.get('granted_minutes',0),
            'pending_minutes':totals.get('pending_minutes',0),
            'blocked_minutes':totals.get('blocked_minutes',0),
            'recovery_minutes':totals.get('recovery_minutes',0),
            'recovery_amount':totals.get('recovery_amount','0.00'),
            'blocked_count':len(comp.get('blocked',[]))}}
