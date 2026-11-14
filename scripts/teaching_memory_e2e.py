"""Live cross-task memory and correction test using a temporary database."""
import sys,tempfile,json,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.core.open_engine import OpenEngine
with tempfile.TemporaryDirectory(prefix='edu-memory-') as tmp:
 e=OpenEngine(Path(tmp)/'db.sqlite')
 a={'class_id':'验收班','course_id':'英语','concept_id':'一般现在时第三人称单数','domain':'英语','objective':'使用第三人称单数动词',
 'question':'改正 He play football every day. 并解释原因。','rubric':'play改成plays，说明主语第三人称单数。',
 'reference':'He plays football every day. He是第三人称单数，因此用plays。'}
 first=e.publish_assignment(a);s=e.create('验收学生',assignment_id=first['assignment_id'])
 r=e.operation(s['session_id'],'a','attempt',{'text':'He play football every day. 我认为主语不影响动词。'})
 assert r['status']=='incorrect',r
 a.update(question='改正 She walk to school every morning. 并解释原因。',reference='She walks to school every morning. She是第三人称单数，因此用walks。',objective='将第三人称单数规则用于新句子',rubric='walk改为walks，并说明主语是第三人称单数。')
 second=e.publish_assignment(a);s2=e.create('验收学生',assignment_id=second['assignment_id'])
 r2=e.operation(s2['session_id'],'b','hint',{})
 traces=e.traces(s2['session_id'])
 assert traces[-1]['context']['learning_memory']
 correction=e.correct(s['session_id'],r['event_id'],'unknown','验收：模拟教师撤回旧判定','fix')
 assert s2['session_id'] in correction['invalidated_sessions']
 reopened=OpenEngine(e.db)
 assert reopened.session(s2['session_id'])['needs_review']
 report={'passed':True,'scope':'real model cross-task memory and correction recovery, not learning gain',
 'first_status':r['status'],'next_feedback':r2['feedback'],'trace':traces,'correction':correction,'memory_after_correction':e.learning_memory('验收学生')}
 Path('reports/teaching-v04-memory-real.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print('Real cross-task memory / correction / reopen passed')
