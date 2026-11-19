"""Real-model smoke run of the course workspace (COURSE_WORKSPACE_DESIGN.md 13.1) on a temporary database.

    EDU_LLM_API_KEY=... python scripts/edu_smoke_course.py --db /tmp/edu-smoke-course.sqlite
    python scripts/edu_smoke_course.py --fake        # scripted model from the tests: checks the script itself

One course, two classes. Class 3 prepares a lesson; the same questions are assigned to classes 3 and 5;
each class's answers are graded and analysed separately (partial, then complete); class 3 corrects its rubric
and regrades while class 5 keeps its version; the next lesson of class 3 reads 本班学情 and class 5's does not;
class 3 gets a follow-up practice assignment that is compared with the first one. Prints what a teacher would
see and a PASS / FAIL / SKIP list at the end. Never point --db at data/edu.sqlite.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edu_core.config import load_settings
from edu_core.llm import Gateway
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace

MATERIAL = """对数函数 y = log_a x（a>0 且 a≠1）的定义域是 (0, +∞)。
当 a>1 时，y = log_a x 在 (0, +∞) 上单调递增；当 0<a<1 时单调递减。
解对数不等式时，先保证真数大于 0（定义域），再利用单调性去掉对数符号，最后取交集。
例：解 log_2(x-1) < 3。由 x-1>0 得 x>1；由单调性 x-1<8 得 x<9；所以 1<x<9。"""

QUESTION = {
    'type': 'calculation',
    'stem': '解不等式 log_2(x-3) < 2。',
    'reference': '由 x-3>0 得 x>3；log_2 单调递增，x-3<4 得 x<7；所以 3<x<7。',
}
RUBRIC = {
    'points': [
        {'description': '写出真数大于零的条件，得到 x>3', 'score': 2},
        {'description': '利用单调性得到 x-3<4，即 x<7', 'score': 2},
        {'description': '取交集得出最终结果 3<x<7', 'score': 2},
    ]
}
CLASS3 = {
    'S01': '先保证真数大于0：x-3>0，所以 x>3。log_2 是增函数，所以 x-3<4，x<7。所以 3<x<7。',
    'S02': '因为 log_2 单调递增，所以 x-3<4，解得 x<7。答：x<7。',
    'S03': 'x-3>0 得 x>3，x-3<4 得 x<7，答案 x<7。',
    'S04': '由单调性 x-3<4，所以 x<7。',
}
CLASS5 = {
    'T01': '定义域 x>3。2^2=4，x-3<4，x<7。综上 3<x<7。',
    'T02': '真数 x-3>0，x>3；又 x-3<4，x<7；所以 3<x<7。',
    'T03': '不会',
}

results = []


def check(name, ok, detail=''):
    state = 'SKIP' if ok is None else ('PASS' if ok else 'FAIL')
    results.append((state, name, detail))
    print(f'[{state}] {name}' + (f' — {detail}' if detail else ''))


def show(title, value):
    print(f'\n=== {title} ===')
    print(json.dumps(value, ensure_ascii=False, indent=1, default=str)[:2500])


def settle_queue(ws, pid, aid):
    """The teacher takes the model's suggestion for every queued grading (0 where there is none)."""
    queue = ws.review_queue(pid, assignment_id=aid)
    for g in queue:
        pts = [
            {
                'id': p['id'],
                'score': p['score'] if p['score'] is not None else (p.get('suggested') or {}).get('score') or 0,
                'error_tag': p.get('error_tag') or (p.get('suggested') or {}).get('error_tag', ''),
            }
            for p in g['content']['points']
        ]
        ws.override(g['id'], pts, note='冒烟测试：采纳建议')
    return len(queue)


def print_gradings(ws, pid, aid):
    for g in ws.gradings(pid, assignment_id=aid):
        c = g['content']
        lost = [f'{p["id"]}:{p["error_tag"]}' for p in c['points'] if p['status'] != 'earned']
        print(
            f'  {c["learner_alias"]} 第{c["question_no"]}题 {c["total"]}/{c["max_total"]} {g["status"]} {lost} risks={c["risks"]}'
        )


def make_workspace(args):
    settings = load_settings(db=args.db, ab_rate=0.0, audit_rate=0.0)
    if args.fake:
        sys.path.insert(0, str(ROOT / 'tests'))
        from edu_fake import FakeLLM
        from test_edu_workflow import HANDLERS

        return Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(0))
    if not settings.api_key:
        sys.exit('EDU_LLM_API_KEY 未设置：export 后重试，或用 --fake 只检查脚本本身')
    llm = Gateway(
        settings.base_url, settings.api_key, settings.models, timeout=settings.timeout, max_tokens=settings.max_tokens
    )
    return Workspace(Store(settings.db), llm, settings)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='/tmp/edu-smoke-course.sqlite')
    ap.add_argument('--fake', action='store_true', help='use the scripted test model')
    args = ap.parse_args()
    if Path(args.db).resolve() == (ROOT / 'data' / 'edu.sqlite').resolve():
        sys.exit('不要在正式数据库上跑冒烟测试')
    for suffix in ('', '-wal', '-shm'):
        Path(args.db + suffix).unlink(missing_ok=True)
    ws = make_workspace(args)
    t0 = time.time()

    # ---- course, classes, first lesson of class 3
    crs = ws.create_course('高一数学·上学期', 'math', grade='高一')
    o3 = ws.link_class(crs['id'], ws.create_class('高一（3）班', list(CLASS3))['id'])
    o5 = ws.link_class(crs['id'], ws.create_class('高一（5）班', list(CLASS5))['id'])
    ws.add_material(None, MATERIAL, filename='讲义：对数函数', course_id=crs['id'])
    t = ws.start_task(
        crs['id'],
        'prep',
        offering_id=o3['id'],
        knowledge_module='对数函数',
        lesson='第 2 课时',
        request='本课重点：解对数不等式时检查定义域',
        lesson_scope='对数函数单调性的应用：解简单对数不等式',
    )
    pid = t['project']['id']
    plan = ws.draft_lesson_plan(pid)
    show('备课方案 ' + plan['context_note'], [o['text'] for o in plan['lesson_plan']['content']['objectives']])
    check('课程资料进入备课', '材料 1 份' in plan['context_note'], plan['context_note'])
    check('草稿不产生课时', ws.sessions(o3['id']) == [])
    ws.confirm(plan['lesson_plan']['id'])
    sessions = ws.sessions(o3['id'])
    check(
        '教案确认后产生一个计划中的课时',
        len(sessions) == 1 and sessions[0]['status'] == 'planned',
        sessions[0]['title'] if sessions else '',
    )

    # ---- questions: one written by the teacher, one generated
    q1 = ws.create_question(pid, QUESTION, RUBRIC)
    ws.confirm(q1['id'])
    gen = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    made = gen['candidates'][0]['questions']
    show(
        f'生成 {len(made)} 道题 ' + gen['context_note'],
        [{k: q['content'][k] for k in ('no', 'stem', 'reference')} for q in made],
    )
    for q in made:
        ws.confirm(q['id'])

    # ---- the same questions for two classes
    a3 = ws.assign(pid, o3['id'])
    a5 = ws.assign(pid, o5['id'])
    check('两个班各有一条布置记录', a3['id'] != a5['id'] and a3['questions'] == a5['questions'] == 1 + len(made))
    imp3 = ws.import_submissions(
        pid, '\n'.join(f'{k},1,{v}' for k, v in CLASS3.items()) + '\nT01,1,串班的作答', assignment_id=a3['id']
    )
    imp5 = ws.import_submissions(pid, '\n'.join(f'{k},1,{v}' for k, v in CLASS5.items()), assignment_id=a5['id'])
    check(
        '别班学生的作答不会导入本班',
        imp3['unmatched_learners'] == ['T01'] and imp3['created'] == 4 and imp5['created'] == 3,
        str(imp3),
    )

    for name, aid in (('3班', a3['id']), ('5班', a5['id'])):
        r = ws.grade_round(pid, assignment_id=aid)
        show(f'{name}批改', r)
        print_gradings(ws, pid, aid)
        check(f'{name}批改没有失败', not r['failed'], str(r['failed'])[:200])
    check(
        '各班只看到本班批改',
        {g['content']['learner_alias'] for g in ws.gradings(pid, assignment_id=a3['id'])} == set(CLASS3),
    )

    # ---- partial, then complete analysis for class 3
    queued = settle_queue(ws, pid, a3['id'])
    first = ws.gradings(pid, assignment_id=a3['id'])[0]
    ws.confirm_gradings(pid, assignment_id=a3['id'], ids=[first['id']])
    try:
        ws.analyze(pid, assignment_id=a3['id'])
        check('批改没全部确认时拒绝完整分析', False)
    except Conflict as e:
        check('批改没全部确认时拒绝完整分析', '有效批改' in str(e), str(e))
    part = ws.analyze(pid, assignment_id=a3['id'], partial=True)
    check(
        '部分分析标明覆盖范围',
        part['analysis']['content'].get('partial') is True,
        part['analysis']['content']['coverage']['label'],
    )
    held = ws.confirm_gradings(pid, assignment_id=a3['id'])
    print(f'复核队列处理 {queued} 份；批量确认 {held}')
    out3 = ws.analyze(pid, assignment_id=a3['id'])
    c3 = out3['analysis']['content']
    show(
        '3班学情分析 ' + out3['context_note'],
        {
            k: c3.get(k)
            for k in ('summary', 'summary_source', 'coverage', 'tag_counts', 'insufficient', 'dropped_patterns')
        },
    )
    show(
        '3班问题模式',
        [{k: p['content'][k] for k in ('name', 'description', 'affected_learners')} for p in out3['patterns']],
    )
    check(
        '完整分析覆盖全部作答',
        not c3.get('partial') and c3['coverage']['valid'] == c3['coverage']['submissions'],
        c3['coverage']['label'],
    )
    check(
        '概述来源与证据核对一致',
        (c3.get('summary_source') == 'model') == (bool(out3['patterns']) and not c3.get('dropped_patterns')),
        f'source={c3.get("summary_source")} patterns={len(out3["patterns"])} dropped={c3.get("dropped_patterns")}',
    )
    for p in out3['patterns']:
        ws.confirm(p['id'])
    ws.confirm(out3['analysis']['id'])

    # ---- class 3 corrects its rubric; class 5 keeps the old version
    settle_queue(ws, pid, a5['id'])
    ws.confirm_gradings(pid, assignment_id=a5['id'])
    q_lin = q1['lineage_id']
    impact = ws.correction_impact(a3['id'], q_lin)
    pts = impact['rubric']['content']['points']
    corrected = ws.correct_rubric(a3['id'], q_lin, {'points': [{**pts[0], 'score': 3}, *pts[1:]]}, note='定义域更重要')
    check('更正评分依据列出5班', [o['class_name'] for o in corrected['other_assignments']] == ['高一（5）班'])
    regrade = ws.grade_round(pid, assignment_id=a3['id'])
    max3 = {
        g['content']['max_total']
        for g in ws.gradings(pid, assignment_id=a3['id'])
        if g['content']['question_lineage'] == q_lin
    }
    max5 = {
        g['content']['max_total']
        for g in ws.gradings(pid, assignment_id=a5['id'])
        if g['content']['question_lineage'] == q_lin
    }
    check(
        '3班按新评分标准重批，5班不变',
        regrade['graded'] == 4 and max3 == {7} and max5 == {6},
        f'graded={regrade["graded"]} 3班满分={max3} 5班满分={max5}',
    )
    settle_queue(ws, pid, a3['id'])
    ws.confirm_gradings(pid, assignment_id=a3['id'])

    # ---- next lesson: class 3 reads its own 学情, class 5 does not
    status = ws.class_status(o3['id'])
    show('3班学情（供下一课时）', status)
    nxt = ws.start_task(crs['id'], 'prep', offering_id=o3['id'], knowledge_module='对数函数', lesson='第 3 课时')
    plan3 = ws.draft_lesson_plan(nxt['project']['id'])
    show(
        '3班第3课时备课方案 ' + plan3['context_note'],
        [o['text'] for o in plan3['lesson_plan']['content']['objectives']],
    )
    if status:
        check('3班下一课时参考本班学情', '本班学情' in plan3['context_note'], plan3['context_note'])
    else:
        check('3班下一课时参考本班学情', None, '3班没有已确认的问题模式或分析')
    t5 = ws.start_task(crs['id'], 'prep', offering_id=o5['id'], knowledge_module='对数函数', lesson='第 3 课时')
    plan5 = ws.draft_lesson_plan(t5['project']['id'])
    check('5班下一课时不读3班学情', '本班学情' not in plan5['context_note'], plan5['context_note'])

    # ---- follow-up practice for class 3, assigned and compared
    if out3['patterns']:
        prac = ws.practice(pid, assignment_id=a3['id'])
        show(f'第 {prac["round"]} 轮再练 ' + prac['context_note'], [q['content']['stem'] for q in prac['questions']])
        if prac['questions']:
            for q in prac['questions']:
                ws.confirm(q['id'])
            b3 = ws.assign(pid, o3['id'], round_=prac['round'], parent_id=a3['id'])
            nos = sorted(ws.assignment(b3['id'])['pinned'], key=lambda x: x['no'])
            rows = '\n'.join(
                f'{k},{p["no"]},先写定义域，再用单调性，最后取交集，得到结果。' for k in CLASS3 for p in nos
            )
            ws.import_submissions(pid, rows, assignment_id=b3['id'])
            ws.grade_round(pid, assignment_id=b3['id'])
            settle_queue(ws, pid, b3['id'])
            ws.confirm_gradings(pid, assignment_id=b3['id'])
            cmp = ws.compare(pid, assignment_id=b3['id'])
            show('两轮对比', cmp['content']['patterns'])
            check('再练布置与上一次布置对比', cmp['assignment_id'] == b3['id'] and cmp['content']['round_a'] == 1)
        else:
            check('再练布置与上一次布置对比', None, '模型没有给出可用的再练题')
    else:
        check('再练布置与上一次布置对比', None, '没有问题模式')

    home = ws.home(crs['id'])
    show(
        '首页',
        {
            'tasks': [(t['title'], t['pending']) for t in home['tasks']],
            'attention': [a['text'] for a in home['attention']],
        },
    )
    check(
        '两个班的数据没有串班',
        {g['content']['learner_alias'] for g in ws.gradings(pid, assignment_id=a5['id'])} == set(CLASS5),
    )

    usage, models = {}, set()
    for p in ws.projects():
        for g in ws.export(p['id'])['generations']:
            models.add(g['model_returned'])
            for k, v in (g['usage'] or {}).items():
                if isinstance(v, int | float):
                    usage[k] = usage.get(k, 0) + v
    show(
        '统计', {'usage': usage, 'models_returned': sorted(m for m in models if m), 'seconds': round(time.time() - t0)}
    )
    print('\n' + '\n'.join(f'[{s}] {n}' + (f' — {d}' if d and s != 'PASS' else '') for s, n, d in results))
    failed = [r for r in results if r[0] == 'FAIL']
    print(f'\n{len(results) - len(failed)}/{len(results)} 通过或跳过')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
