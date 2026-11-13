"""Summarize actual artifacts, including pre-fix failures, without outcome claims."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]

def main():
    e2e=json.loads((ROOT/'reports/runtime-e2e.json').read_text())
    recovery=json.loads((ROOT/'reports/runtime-recovery.json').read_text())
    suite=ET.parse(ROOT/'reports/runtime-tests.xml').getroot().find('testsuite')
    generations=[t for c in e2e['cases'] for t in c['traces'] if 'candidate' in t]
    record={'version':'0.2.0','policy_version':'evidence-policy-v1',
        'unit_tests':dict(suite.attrib),'real_model_branches':[
            {k:c[k] for k in ('name','expected_action','actual_action','passed','seconds')} for c in e2e['cases']],
        'generated_interventions':len(generations),
        'guard_fallbacks':sum(t['validation'].get('replacement',False) for t in generations),
        'recovery_passed':recovery['passed'],
        'passed':e2e['passed'] and recovery['passed'] and int(suite.get('failures'))==0 and int(suite.get('errors'))==0,
        'scope':'API-only scripted tutoring, state safety and deterministic policy replay; not a learning efficacy study',
        'known_limits':['fixed course and finite checks','lexical guard not semantic proof',
                        'model interpretation can be wrong despite a valid quote','no teacher blind rating',
                        'no full database event replay','SQLite serialized writes'],
        'pre_fix_findings':[
            'Gradio SSE connections delayed shutdown; added 30-second graceful shutdown timeout',
            'Full math in conceptual-generation context induced unrequested computation and answer disclosure; guarded and filtered',
            'Model repeatedly ignored JSON wrapper for verbalization; changed only verbalization to guarded plain text'],
        'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (ROOT/'harness').glob('*.py')}}
    (ROOT/'reports/runtime-acceptance.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps({k:record[k] for k in ('passed','generated_interventions','guard_fallbacks','recovery_passed')},ensure_ascii=False))

if __name__=='__main__':main()
