"""Live evaluation of the hint ladder and full explanations (real models, temporary database).

    OMNIEDU_REVIEW_BASE_URL=http://127.0.0.1:8002/v1 \
    .envs/harness/bin/python scripts/eval_hint_ladder.py --repeats 3 --tag after

For each task and repeat: (a) a wrong attempt followed by up to three hint requests;
(b) in a separate session, a wrong attempt followed by a request for a full explanation.
Reports, per hint level, whether the learner received the model's hint, a server
template, or a generic fallback; how often the session was handed to a teacher;
whether any hint contained the final answer; and whether explanations reached it.
Each turn's rejection reasons and drafts are kept in the JSON report.
Results are model outputs on a handful of tasks, not a measure of learning.
"""

import argparse
import json
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.evidence import answer_numbers, numbers
from harness.core.open_engine import OpenEngine

TASKS = [
    dict(
        name='speed-zh',
        task_type='problem_solving',
        domain='数学',
        objective='能根据路程和时间求平均速度，并写出计算过程和单位',
        question='小明骑车 3 小时行了 45 千米，他的平均速度是多少千米每小时？请写出计算过程和单位。',
        rubric='列出“速度 = 路程 ÷ 时间”，代入数据计算，结果带单位。',
        reference='45 ÷ 3 = 15，平均速度是 15 千米/小时。',
        wrong='平均速度等于路程乘以时间，45 × 3 = 135，所以平均速度是 135 千米每小时。',
    ),
    dict(
        name='force-zh',
        task_type='problem_solving',
        domain='物理',
        objective='能用牛顿第二定律由力和质量求加速度',
        question='一个质量为 2 kg 的物体受到 10 N 的合力，它的加速度是多少？写出公式和单位。',
        rubric='写出 a = F / m，代入数据，结果带单位 m/s²。',
        reference='a = F / m = 10 ÷ 2 = 5 m/s²。',
        wrong='a = F × m = 10 × 2 = 20 m/s²',
    ),
    dict(
        name='tickets-en',
        task_type='problem_solving',
        domain='Math',
        objective='Solve the word problem with correct reasoning',
        question='Connie had some tickets. She spent half of them on a koala, 10 on earbuds and 15 on bracelets. '
        'How many tickets did she have at first?',
        rubric='The final answer must be 50 and every step must be correct.',
        reference='Earbuds and bracelets cost 10 + 15 = 25 tickets, which is the other half, so she had 25 x 2 = 50 tickets.',
        wrong='Earbuds cost 10 tickets, which is half of the total. So she had 10 x 2 = 20 tickets.',
    ),
    dict(
        name='moon-zh',
        task_type='concept',
        domain='科学',
        objective='能用日、地、月的相对位置解释月相变化',
        question='为什么我们看到的月亮形状会在一个月里变化？请用太阳、地球和月球的位置来解释。',
        rubric='说明月球本身不发光、反射太阳光；月球绕地球运动时，被照亮的半球朝向地球的部分不断变化。',
        reference='月球不发光，反射太阳光；月球绕地球运行，被照亮的半球朝向地球的部分不断变化，所以看到的形状变化。',
        wrong='因为地球的影子挡住了月亮的一部分，挡住多少就缺多少。',
    ),
]
HINTS = ('scaffold_concept', 'scaffold_step', 'scaffold_example')


def assignment(task):
    fields = {k: task[k] for k in ('task_type', 'domain', 'objective', 'question', 'rubric', 'reference')}
    return fields | {'class_id': 'eval', 'course_id': task['name'], 'concept_id': task['name']}


def act(engine, sid, kind, text=''):
    version = engine.session(sid)['version']
    return engine.operation(sid, uuid.uuid4().hex, kind, {'text': text}, version)


def why(engine, sid):
    """The audit trail of the latest turn: rejection reasons, reviewer verdicts and drafts."""
    v = engine.traces(sid)[-1].get('output_validation', {})
    first = v.get('first_draft') or {}
    return {
        'reasons': v.get('reasons', []),
        'first_reasons': first.get('reasons', []),
        'rewrite_model': v.get('rewrite_model'),
        'review': (v.get('review') or {}).get('reason', '')[:200],
        'cross_review': (v.get('cross_review') or {}).get('reason', '')[:200],
        'first_draft': (first.get('candidate') or '')[:300],
        'candidate': (v.get('candidate') or '')[:300],
    }


def guarded(fn):
    """Record a failed run (e.g. malformed model output) instead of aborting the whole evaluation."""

    def run(*args):
        try:
            return fn(*args)
        except Exception as error:  # reported per run in the summary
            return {'error': f'{type(error).__name__}: {error}'}

    return run


@guarded
def ladder_run(engine, aid, task):
    s = engine.create(f'ladder-{uuid.uuid4().hex[:6]}', assignment_id=aid)
    act(engine, s['session_id'], 'attempt', task['wrong'])
    hints = []
    for _ in HINTS:
        last = engine.session(s['session_id'])['history'][-1]
        if last.get('action') == 'teacher_review':
            break
        turn = act(engine, s['session_id'], 'hint')['session']['history'][-1]
        hints.append(
            {k: turn.get(k) for k in ('action', 'delivered_action', 'feedback')} | why(engine, s['session_id'])
        )
    return hints


@guarded
def explain_run(engine, aid, task):
    s = engine.create(f'explain-{uuid.uuid4().hex[:6]}', assignment_id=aid)
    act(engine, s['session_id'], 'attempt', task['wrong'])
    turn = act(engine, s['session_id'], 'explain')['session']['history'][-1]
    return {k: turn.get(k) for k in ('action', 'delivered_action', 'feedback')} | why(engine, s['session_id'])


def classify(hint):
    delivered = hint.get('delivered_action') or hint['action']
    if hint['action'] == 'teacher_review':
        return 'teacher_review'
    if delivered == hint['action']:
        return 'model'
    if delivered.endswith('_template'):
        return 'template'
    return 'generic_fallback'


def summarize(jobs, ladders, explains):
    runs = []
    for (task, repeat), hints, explanation in zip(jobs, ladders, explains, strict=True):
        answer = answer_numbers(task)
        run = {'task': task['name'], 'repeat': repeat, 'errors': [], 'hints': [], 'explanation': None}
        if isinstance(hints, dict):
            run['errors'].append('ladder: ' + hints['error'])
        else:
            run['hints'] = [
                h | {'outcome': classify(h), 'leaks_answer': bool(answer) and answer <= numbers(h.get('feedback'))}
                for h in hints
            ]
        if 'error' in explanation:
            run['errors'].append('explain: ' + explanation['error'])
        else:
            run['explanation'] = explanation | {
                'reaches_answer': answer <= numbers(explanation.get('feedback')),
                'delivered': explanation.get('delivered_action') or explanation['action'],
            }
        runs.append(run)

    levels = {}
    for run in runs:
        for level, hint in zip(HINTS, run['hints'], strict=False):
            levels.setdefault(level, []).append(hint['outcome'])
    ladders_ok = [r for r in runs if not any(e.startswith('ladder') for e in r['errors'])]
    completed = [len(r['hints']) == 3 and r['hints'][-1]['outcome'] != 'teacher_review' for r in ladders_ok]
    explained = [r for r in runs if r['explanation']]
    numeric = [r for r in explained if answer_numbers(next(t for t in TASKS if t['name'] == r['task']))]
    by_model = sum(r['explanation']['delivered'] == 'explain' for r in explained)
    reaching = sum(r['explanation']['reaches_answer'] for r in numeric)
    summary = {
        'runs': len(runs),
        'errors': sum(len(r['errors']) for r in runs),
        'ladder_completed': f'{sum(completed)}/{len(ladders_ok)}',
        'reached_teacher_review': sum(any(h['outcome'] == 'teacher_review' for h in r['hints']) for r in runs),
        'per_level': {level: {o: outs.count(o) for o in sorted(set(outs))} for level, outs in levels.items()},
        'hints_leaking_answer': sum(h['leaks_answer'] for r in runs for h in r['hints']),
        'explanations_delivered_by_model': f'{by_model}/{len(explained)}',
        'explanations_reaching_answer_numeric_tasks': f'{reaching}/{len(numeric)}',
    }
    return summary, runs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--tag', default='run')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--out', type=Path, default=Path('reports/hint-ladder'))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        engine = OpenEngine(Path(tmp) / 'eval.sqlite')
        aids = {t['name']: engine.publish_assignment(assignment(t))['assignment_id'] for t in TASKS}
        jobs = [(t, r) for t in TASKS for r in range(args.repeats)]
        with ThreadPoolExecutor(args.workers) as pool:
            ladders = list(pool.map(lambda job: ladder_run(engine, aids[job[0]['name']], job[0]), jobs))
            explains = list(pool.map(lambda job: explain_run(engine, aids[job[0]['name']], job[0]), jobs))
    summary, runs = summarize(jobs, ladders, explains)
    summary['seconds'] = round(time.time() - started)
    report = {'tag': args.tag, 'scope': 'live model outputs on four tasks; not a learning-outcome measure'}
    report |= {'summary': summary, 'runs': runs}
    (args.out / f'{args.tag}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
