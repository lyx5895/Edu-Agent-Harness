"""Summarize reproducible test artifacts without inferring learning outcomes."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]

def main():
 e=json.loads((ROOT/'reports/open-task-e2e.json').read_text())
 r=json.loads((ROOT/'reports/open-task-recovery.json').read_text())
 suite=ET.parse(ROOT/'reports/open-task-tests.xml').getroot().find('testsuite')
 traces=[t for c in e['cases'] for t in c.get('traces',[])]
 interventions=[t['output_validation'] for t in traces if 'output_validation' in t]
 report={'version':'0.3.0','passed':e['passed'] and r['passed'] and not int(suite.get('failures')) and not int(suite.get('errors')),
   'unit_tests':dict(suite.attrib),
   'real_cases':[{k:c.get(k) for k in ('name','passed','seconds','generated_check_fixture_status','error')} for c in e['cases']],
   'image_material_and_restart':r['passed'],
   'feedback_checks':len(interventions),'feedback_fallbacks':sum(v['fallback'] for v in interventions),
   'core_modules':['core/tasks.py','core/open_engine.py','llm/open_model.py','resources/toolbox.py'],
   'fixed_courses_required':False,'example_compatibility':'gateway.py -> legacy engine only for explicit skill or legacy session',
   'sources':'REFERENCES.txt',
   'limits':['Model evaluations and same-model check review can be wrong',
             'No teacher blind evaluation or learner outcome experiment',
             'Material files: UTF-8 txt/md; images require transcription confirmation',
             'Context bounded to recent evidence and retrieved text, not entire documents',
             'Verifier coverage is optional; unsupported domains use labeled model rubric assessment',
             'Single-server demo identity, no multi-user isolation'],
   'prior_failure_artifacts':['open-task-e2e-initial-failures.json','open-task-e2e-before-source-and-action-fix.json','open-check-rejection.json','open-task-e2e-before-reference-validation.json'],
   'code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'harness').rglob('*.py')}}
 (ROOT/'reports/open-task-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({k:report[k] for k in ('passed','feedback_checks','feedback_fallbacks','image_material_and_restart')},ensure_ascii=False))
if __name__=='__main__':main()
