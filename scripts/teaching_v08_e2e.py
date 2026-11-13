"""Live regression for v0.8: PDF materials with page citations, translation of Chinese
candidates, 27B cross-review and the v5 stall escalation. Uses a temporary database.
Needs OmniEdu-9B on :8000 and Qwen3.5-27B on :8002."""
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from harness.materials import read_material
from harness.open_engine import OpenEngine
from harness.open_model import OpenModel
from harness.tasks import language
from test_retrieval import pdf
from teaching_v071_e2e import EN_PROBLEM, EN_WRONG, ZH_PROBLEM

CJK = re.compile(r'[\u4e00-\u9fff]')


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else 'reports/teaching-v08-real.json')
    report = {'scope': 'scripted behaviour checks on the live models, not learning outcomes', 'cases': []}
    with tempfile.TemporaryDirectory(prefix='edu-v08-') as tmp:
        reviewer = OpenModel('http://127.0.0.1:8002/v1', 'qwen3.5-27b')
        e = OpenEngine(Path(tmp) / 'test.sqlite', reviewer=reviewer)
        v5 = OpenEngine(Path(tmp) / 'v5.sqlite', policy='open-task-policy-v5')

        def run(name, fn):
            start = time.time()
            try:
                row = {'name': name, 'passed': True, 'details': fn()}
            except Exception as error:
                row = {'name': name, 'passed': False, 'error': f'{type(error).__name__}: {error}'}
            row['seconds'] = round(time.time() - start, 2)
            report['cases'].append(row)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(name, row['passed'], row.get('error', ''), row['seconds'], flush=True)

        def pdf_pages():
            path = Path(tmp) / 'notes.pdf'
            path.write_bytes(pdf(['Photosynthesis happens in chloroplasts and makes glucose from light, water and carbon dioxide.',
                                  'Newton second law: the net force on an object equals its mass times its acceleration, F = m a.']))
            s = e.create('pdf-student', 'Explain what Newton second law says about force and acceleration.',
                         material=read_material(path), mode='learn')
            assert s['task']['source_pages'] == [2], s['task']
            return {'question': s['task']['question'], 'source_pages': s['task']['source_pages']}
        run('pdf_material_cites_page', pdf_pages)

        def translate():
            text = e.model.translate('你计算了总量，但减去取走的部分得到的是剩余量。这一步的依据是什么？')
            assert language(text) == 'en' and not CJK.search(text), text
            return {'translation': text}
        run('chinese_candidate_is_translated', translate)

        def cross_review():
            a = e.publish_assignment(EN_PROBLEM)
            s = e.create('xr-student', assignment_id=a['assignment_id'])
            e.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': EN_WRONG})
            turn = [t for t in e.traces(s['session_id']) if t.get('operation') == 'attempt'][-1]
            with e.connect() as c:
                payload = json.loads(c.execute("SELECT payload FROM learning_events WHERE session=? AND kind='turn' ORDER BY id DESC",
                                               (s['session_id'],)).fetchone()[0])
            cross = payload.get('cross_review')
            assert cross and cross['agreed'] and payload['status'] == 'incorrect', (payload['status'], cross)
            return {'cross_review': cross, 'feedback_review': turn['output_validation'].get('cross_review')}
        run('reviewer_confirms_wrong_answer', cross_review)

        def stall_escalation():
            a = v5.publish_assignment(ZH_PROBLEM)
            s = v5.create('stubborn', assignment_id=a['assignment_id'])
            actions = [v5.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': '平均速度等于路程乘以时间，45 × 3 = 135。'})['action']]
            for _ in range(3):
                actions.append(v5.operation(s['session_id'], uuid.uuid4().hex, 'reply', {'text': '我觉得我没错，就是135。'})['action'])
            assert any(x.startswith('scaffold') for x in actions), actions
            return {'actions': actions}
        run('v5_escalates_hints_for_stubborn_student', stall_escalation)

    report['passed'] = all(c['passed'] for c in report['cases'])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
