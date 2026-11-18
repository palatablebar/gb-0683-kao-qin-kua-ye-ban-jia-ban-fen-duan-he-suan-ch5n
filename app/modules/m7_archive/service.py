from app.modules.m7_archive.export_out import checksum

def execute(state):
    comp=state.get('comp',{})
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+len(comp.get('ledger',[]))+len(comp.get('blocked',[])),
        'ready':state.get('reconcile',{}).get('balanced',False)}
    return state
