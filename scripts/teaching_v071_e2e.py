"""Live regression for v0.7.1 behaviour: locate policy, reply language, grounding,
hint ladder, per-turn model budget and concurrent students. Uses a temporary database."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.open_engine import OpenEngine

CJK = re.compile(r'[\u4e00-\u9fff]')
EN_PROBLEM = dict(class_id='Live', course_id='math', concept_id='fractions', domain='Math', task_type='problem_solving',
                  objective='Solve the word problem with correct reasoning',
                  question='Connie had some tickets. She spent half of them on a koala, 10 on earbuds and 15 on bracelets. How many tickets did she have at first?',
                  rubric='The final answer must be 50 and every step must be correct.',
                  reference='Earbuds and bracelets cost 10 + 15 = 25 tickets, which is the other half, so she had 25 x 2 = 50 tickets.')
EN_WRONG = 'Earbuds cost 10 tickets, which is half of the total. So she had 10 x 2 = 20 tickets.'
ZH_PROBLEM = dict(class_id='Live', course_id='数学', concept_id='speed', domain='数学', task_type='problem_solving',
                  objective='用路程和时间求平均速度', question='小明骑车3小时行了45千米，他的平均速度是多少千米每小时？',
                  rubric='用路程除以时间，答案为15千米每小时，单位正确。', reference='45 ÷ 3 = 15，平均速度是15千米每小时。')
ZH_WRONG = '平均速度等于路程乘以时间，45 × 3 = 135，所以是135千米每小时。'


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else 'reports/teaching-v071-real.json')
    report = {'scope': 'scripted behaviour checks on the live model, not learning outcomes', 'cases': []}
    with tempfile.TemporaryDirectory(prefix='edu-v071-') as tmp:
        e = OpenEngine(Path(tmp) / 'test.sqlite')

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

        def attempt(problem, learner, text):
            a = e.publish_assignment(problem)
            s = e.create(learner, assignment_id=a['assignment_id'])
            before = len(e.model.calls)
            r = e.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': text})
            trace = [t for t in e.traces(s['session_id']) if t.get('operation') == 'attempt'][-1]
            return s, r, trace, len(e.model.calls) - before

        def english_locate():
            _, r, trace, calls = attempt(EN_PROBLEM, 'en-student', EN_WRONG)
            assert r['status'] == 'incorrect', r['status']
            assert r['action'] == 'locate', r['action']
            assert not CJK.search(r['feedback']), r['feedback']
            assert '50' not in r['feedback'], r['feedback']
            assert calls <= 12, calls
            return {'feedback': r['feedback'], 'delivered': trace['output_validation'].get('delivered_action'),
                    'reasons': trace['output_validation'].get('reasons'), 'model_calls': calls,
                    'evidence_quote': trace['state_after']['hypotheses'][-1]['quote']}
        run('english_wrong_attempt_is_located_in_english', english_locate)

        def chinese_locate():
            _, r, trace, calls = attempt(ZH_PROBLEM, 'zh-student', ZH_WRONG)
            assert r['status'] == 'incorrect' and r['action'] == 'locate', (r['status'], r['action'])
            assert CJK.search(r['feedback']), r['feedback']
            assert '15千米' not in r['feedback'] and '= 15' not in r['feedback'], r['feedback']
            return {'feedback': r['feedback'], 'delivered': trace['output_validation'].get('delivered_action'),
                    'reasons': trace['output_validation'].get('reasons'), 'model_calls': calls}
        run('chinese_wrong_attempt_is_located_in_chinese', chinese_locate)

        def hint_ladder():
            a = e.publish_assignment(ZH_PROBLEM)
            s = e.create('hinter', assignment_id=a['assignment_id'])
            actions = [e.operation(s['session_id'], uuid.uuid4().hex, 'hint', {'text': '不会做'})['action'] for _ in range(3)]
            assert actions == ['scaffold_concept', 'scaffold_step', 'scaffold_example'], actions
            return {'actions': actions}
        run('hint_ladder_escalates', hint_ladder)

        def concurrent():
            a = e.publish_assignment(ZH_PROBLEM)
            sessions = [e.create(f'class-{i}', assignment_id=a['assignment_id']) for i in range(8)]
            def go(item):
                i, s = item
                text = ZH_WRONG if i % 2 else '45 ÷ 3 = 15，平均速度是15千米每小时。'
                return e.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': text})['status']
            start = time.time()
            with ThreadPoolExecutor(8) as pool:
                statuses = list(pool.map(go, enumerate(sessions)))
            assert all(x in ('correct', 'incorrect', 'unknown') for x in statuses), statuses
            return {'statuses': statuses, 'wall_seconds': round(time.time() - start, 2)}
        run('eight_students_submit_concurrently', concurrent)

    report['passed'] = all(c['passed'] for c in report['cases'])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
