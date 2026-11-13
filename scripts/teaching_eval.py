"""Small authored grading regression set. Not a teacher blind study or learning-gain test.
Runs the live model on a temporary DB, never writes student records to the app DB.
"""
import json
import argparse
from pathlib import Path
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.open_engine import OpenEngine


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='reports/teaching-v05-real.json')
    parser.add_argument('--compare',action='store_true',help='Compare raw model grading against the harness on the same authored cases')
    args=parser.parse_args()
    cases=json.loads((ROOT/'evaluations/teaching_cases.json').read_text())
    report={'scope':'authored grading regression; not learning outcomes','cases':[]}
    with tempfile.TemporaryDirectory(prefix='edu-eval-') as tmp:
        engine=OpenEngine(Path(tmp)/'eval.sqlite')
        for case in cases:
            start=time.time()
            row={'name':case['name'],'expected':case['expected']}
            try:
                if args.compare:
                    from harness.tasks import Assessment
                    baseline=engine.model.structured(Assessment,
                        '评估学生作答是否符合题目和评分要求，返回status、confidence、evidence_quote、observation、hypothesis。evidence_quote逐字来自学生作答。',
                        json.dumps({k:case[k] for k in ('question','rubric','reference','answer')},ensure_ascii=False)).model_dump()
                    row['raw_model_status']=baseline['status']
                    row['raw_model_matches']=baseline['status']==case['expected']
                a=engine.publish_assignment({k:case[k] for k in ('domain','concept_id','question','rubric','reference')} |
                    {'class_id':'evaluation','course_id':case['domain'],'objective':case['question']})
                s=engine.create('eval-'+case['name'],assignment_id=a['assignment_id'])
                r=engine.operation(s['session_id'],'answer','attempt',{'text':case['answer']})
                row.update(actual=r['status'],passed=r['status']==case['expected'],feedback=r['feedback'],
                           trace=engine.traces(s['session_id']))
            except Exception as e:row.update(passed=False,error=str(e))
            row['seconds']=time.time()-start
            report['cases'].append(row)
            report['passed']=all(x['passed'] for x in report['cases'])
            report['false_acceptances']=sum(x.get('actual')=='correct' and x['expected']!='correct' for x in report['cases'])
            (ROOT/args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(row['name'],row['passed'],row.get('actual',row.get('error')),flush=True)
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
