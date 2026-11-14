"""Live rubric -> review assignment -> curriculum progression -> correction recovery.
Scripted student answers; measures execution and grading, not learning gains.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.core.open_engine import OpenEngine


def main():
    report={'passed':False,'scope':'live scripted teaching workflow; not learner outcome study','steps':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='edu-v05-') as tmp:
            e=OpenEngine(Path(tmp)/'db.sqlite')
            base=dict(class_id='验收班',course_id='代数',concept_id='linear',domain='数学',objective='解一元一次方程',
                      question='解方程 2*x+3=7，并解释依据。',rubric='结果与等式性质解释均需正确',reference='x=2；等式两边同时减3再除以2。',
                      criteria=[dict(id='answer',kind='algebra',description='正确求解方程'),dict(id='reason',kind='explanation',description='说明等式两边进行相同运算')])
            aid=e.publish_assignment(base)['assignment_id'];s=e.create('A',assignment_id=aid)
            r=e.operation(s['session_id'],'one','attempt',{'text':'x=2'})
            report['steps'].append({'name':'missing_explanation','result':r})
            assert r['status']=='incomplete' and r['criteria_results'][0]['status']=='correct'
            r=e.operation(s['session_id'],'two','attempt',{'text':'x=2；等式两边同时减去3，得到2*x=4，再同时除以2。'})
            report['steps'].append({'name':'complete_explanation','result':r})
            assert r['status']=='correct'
            review=dict(base,question='解方程 3*x+1=10。',reference='x=3',rubric='正确求解',criteria=base['criteria'][:1])
            check_id=e.publish_assignment(review)['assignment_id']
            next_id=e.publish_assignment(dict(review,concept_id='next',objective='后续目标',question='解方程 4*x+1=17。',reference='x=4'))['assignment_id']
            path=e.publish_path(dict(class_id='验收班',course_id='代数',title='代数路径',nodes=[
                dict(concept_id='linear',title='方程基础',assignment_id=aid),dict(concept_id='next',title='后续目标',assignment_id=next_id,prerequisite_ids=['linear'])]))
            follow=e.assign_followups(dict(class_id='验收班',assignment_id=check_id,learners=['A'],kind='review',due_at=time.time(),reason='检查能否迁移到新题'),'schedule')[0]
            assert e.next_step(path['path_id'],'A')['action']=='followup'
            s2=e.create('A',followup_id=follow['id'])
            r2=e.operation(s2['session_id'],'three','attempt',{'text':'x=3'})
            report['steps'].append({'name':'independent_review','result':r2})
            assert r2['evidence']=='independent_verified'
            assert e.followups('验收班','A')[0]['status']=='completed'
            assert e.next_step(path['path_id'],'A')['concept_id']=='next'
            e.correct(s2['session_id'],r2['event_id'],'unknown','验收模拟教师撤回证据','fix')
            reopened=OpenEngine(e.db)
            assert reopened.followups('验收班','A')[0]['status']=='needs_recheck'
            assert reopened.next_step(path['path_id'],'A')['concept_id']=='linear'
            report['memory']=reopened.learning_memory('A')
            report['passed']=True
    except Exception as exc:
        report['error']=str(exc)
        raise
    finally:
        (ROOT/'reports/teaching-v05-workflow-real.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('v0.5 workflow:',report['passed'],report.get('error',''),flush=True)


if __name__=='__main__':main()
