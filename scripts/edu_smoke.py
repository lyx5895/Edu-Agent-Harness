"""Real-model smoke run of the teacher loop on a temporary database.

    EDU_LLM_API_KEY=... python scripts/edu_smoke.py --db /tmp/edu-smoke.sqlite

Round 1: material -> objectives -> questions (with self-check) -> 6 student answers -> grading with
review -> analysis -> practice questions. Prints what a teacher would see at each step.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edu_core.config import load_settings
from edu_core.llm import Gateway
from edu_core.storage import Store
from edu_core.workflow import Workspace

MATERIAL = """对数函数 y = log_a x（a>0 且 a≠1）的定义域是 (0, +∞)。
当 a>1 时，y = log_a x 在 (0, +∞) 上单调递增；当 0<a<1 时单调递减。
解对数不等式时，先保证真数大于 0（定义域），再利用单调性去掉对数符号，最后取交集。
例：解 log_2(x-1) < 3。由 x-1>0 得 x>1；由单调性 x-1<8 得 x<9；所以 1<x<9。"""

ANSWERS = {
    'S01': '先保证真数大于0：x-3>0，所以 x>3。log_2 是增函数，所以 x-3<4，x<7。所以 3<x<7。',
    'S02': '因为 log_2 单调递增，所以 x-3<4，解得 x<7。答：x<7。',
    'S03': '由 log_2(x-3)<2 得 x-3<2，所以 x<5，又 x>3，所以 3<x<5。',
    'S04': 'x-3>0 得 x>3，x-3<4 得 x<7，答案 x<7。',
    'S05': '定义域 x>3。2^2=4，x-3<4，x<7。综上 3<x<7。',
    'S06': '不会',
}


def show(title, value):
    print(f'\n=== {title} ===')
    print(json.dumps(value, ensure_ascii=False, indent=1)[:3000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='/tmp/edu-smoke.sqlite')
    args = ap.parse_args()
    Path(args.db).unlink(missing_ok=True)
    settings = load_settings(db=args.db, ab_rate=0.0, audit_rate=0.0)
    llm = Gateway(
        settings.base_url, settings.api_key, settings.models, timeout=settings.timeout, max_tokens=settings.max_tokens
    )
    ws = Workspace(Store(settings.db), llm, settings)
    t0 = time.time()

    p = ws.create_project(
        '高一数学｜对数函数｜第2课时', 'math', grade='高一', notes='本课重点：解对数不等式时检查定义域'
    )
    pid = p['id']
    ws.add_material(pid, MATERIAL, filename='讲义')
    objs = ws.draft_objectives(pid)
    show('起草的目标 ' + objs['context_note'], [o['content'] for o in objs['objectives']])
    for o in objs['objectives']:
        ws.confirm(o['id'])

    question = {
        'type': 'calculation',
        'stem': '解不等式 log_2(x-3) < 2。',
        'reference': '由 x-3>0 得 x>3；log_2 单调递增，x-3<4 得 x<7；所以 3<x<7。',
    }
    rubric = {
        'points': [
            {'description': '写出真数大于零的条件，得到 x>3', 'score': 2},
            {'description': '利用单调性得到 x-3<4，即 x<7', 'score': 2},
            {'description': '取交集得出最终结果 3<x<7', 'score': 2},
        ]
    }
    teacher_q = ws.create_question(pid, question, rubric)
    ws.confirm(teacher_q['id'])

    gen = ws.generate_questions(pid, types=['single_choice', 'fill_blank', 'calculation'], count=1, ab=False)
    made = gen['candidates'][0]['questions']
    show(
        f'生成 {len(made)} 道题 ' + gen['context_note'],
        [
            {
                **{k: q['content'][k] for k in ('no', 'type', 'stem', 'answer_key', 'reference')},
                'checks': q['content']['checks'],
                'points': q['rubric']['content']['points'],
            }
            for q in made
        ],
    )

    ws.add_learners(pid, list(ANSWERS))
    csv_rows = '\n'.join(f'{k},1,{v}' for k, v in ANSWERS.items())
    show('导入作答', ws.import_submissions(pid, csv_rows))
    show('批改', ws.grade_round(pid))
    for g in ws.gradings(pid):
        c = g['content']
        print(f'\n{c["learner_alias"]}: {c["total"]}/{c["max_total"]} risks={c["risks"]}')
        for pt in c['points']:
            print(
                f'   {pt["id"]} {pt["status"]:12s} {pt["score"]} [{pt["source"]}] tag={pt["error_tag"]!r} quote={pt["evidence_quote"][:40]!r} review={pt.get("review", {}).get("agreed")}'
            )
    queue = ws.review_queue(pid)
    print(f'\n复核队列：{len(queue)} 份')
    for g in queue:  # teacher accepts the model suggestion where there is one, else 0
        pts = [
            {
                'id': p['id'],
                'score': p['score'] if p['score'] is not None else (p.get('suggested') or {}).get('score') or 0,
                'error_tag': p.get('error_tag') or (p.get('suggested') or {}).get('error_tag', ''),
            }
            for p in g['content']['points']
        ]
        ws.override(g['id'], pts, note='冒烟测试：采纳建议')
    show('批量确认', ws.confirm_gradings(pid))

    out = ws.analyze(pid)
    show(
        '学情分析 ' + out['context_note'],
        {k: out['analysis']['content'].get(k) for k in ('summary', 'tag_counts', 'insufficient', 'dropped_patterns')},
    )
    show(
        '问题模式',
        [
            {k: pt['content'][k] for k in ('name', 'description', 'affected_learners', 'evidence')}
            for pt in out['patterns']
        ],
    )
    for pt in out['patterns']:
        ws.confirm(pt['id'])
    if out['patterns']:
        prac = ws.practice(pid)
        show(
            f'第 {prac["round"]} 轮巩固练习 ' + prac['context_note'],
            [
                {**{k: q['content'][k] for k in ('stem', 'target_patterns')}, 'checks': q['content']['checks']}
                for q in prac['questions']
            ],
        )

    export = ws.export(pid)
    usage = {}
    for g in export['generations']:
        for k, v in (g['usage'] or {}).items():
            if isinstance(v, int | float):
                usage[k] = usage.get(k, 0) + v
    show('导出统计', {**export['manifest']['counts'], 'usage': usage, 'seconds': round(time.time() - t0)})
    print('\nmodels returned:', sorted({g['model_returned'] for g in export['generations']}))


if __name__ == '__main__':
    main()
