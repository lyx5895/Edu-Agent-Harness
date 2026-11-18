"""Grading benchmark for edu_core.grading.grade (DeepSeek grader + Qwen reviewer by default).

    EDU_LLM_API_KEY=... python scripts/edu_eval_grading.py --data bench/data/stepverify-zh.jsonl \
        --out reports/edu/grading-stepverify-zh.json

Each item becomes one rubric point "solution and final answer fully correct". Outcome per item:
earned -> passed, missed -> failed, undetermined -> routed to the teacher.
"""

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edu_core.config import load_settings
from edu_core.grading import grade
from edu_core.llm import Gateway, ModelUnavailable


def item_case(item):
    subject = {'物理': 'physics', '化学': 'chemistry', '生物': 'biology', '语文': 'chinese', '英语': 'english'}.get(
        item.get('subject'), 'math'
    )
    question = {
        'type': 'calculation' if subject in ('math', 'physics', 'chemistry') else 'short_answer',
        'stem': item['question'],
        'reference': item['reference_solution'],
    }
    rubric = {
        'points': [
            {
                'id': 'P1',
                'description': item.get('rubric') or '解答过程与最终答案完全正确，没有任何计算或推理错误',
                'score': 1,
            }
        ]
    }
    return subject, question, rubric, item['student_chat_solution'][:1600], str(item['is_error']) == 'True'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='bench/data/stepverify-zh.jsonl')
    ap.add_argument('--out', default='reports/edu/grading-stepverify-zh.json')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=12)
    args = ap.parse_args()
    items = [json.loads(line) for line in Path(args.data).read_text(encoding='utf-8').splitlines() if line.strip()]
    if args.limit:
        items = items[: args.limit]
    s = load_settings()
    llm = Gateway(s.base_url, s.api_key, s.models, timeout=s.timeout, max_tokens=s.max_tokens)
    rng = random.Random(0)

    def run(item):
        subject, question, rubric, answer, is_error = item_case(item)
        t = time.time()
        try:
            result, metas = grade(
                llm, subject=subject, question=question, rubric=rubric, answer=answer, review=True, rng=rng
            )
        except ModelUnavailable as e:
            return {'id': item['id'], 'is_error': is_error, 'error': str(e)}
        p = result['points'][0]
        outcome = {'earned': 'passed', 'missed': 'failed', 'partial': 'failed'}.get(p['status'], 'teacher')
        return {
            'id': item['id'],
            'subject': item.get('subject', '数学'),
            'is_error': is_error,
            'outcome': outcome,
            'risks': result['risks'],
            'review': p.get('review'),
            'calls': [m['task'] for m in metas],
            'models': [m.get('model_returned') for m in metas],
            'seconds': round(time.time() - t, 1),
            'tokens': sum((m.get('usage') or {}).get('total_tokens', 0) for m in metas),
        }

    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(run, items))
    ok = [r for r in rows if 'outcome' in r]
    err = [r for r in ok if r['is_error']]
    good = [r for r in ok if not r['is_error']]

    def rate(xs, outcome):
        return round(sum(r['outcome'] == outcome for r in xs) / max(len(xs), 1), 4)

    summary = {
        'data': args.data,
        'models': s.models,
        'items': len(rows),
        'failed_calls': len(rows) - len(ok),
        'error_items_passed': rate(err, 'passed'),
        'correct_items_failed': rate(good, 'failed'),
        'routed_to_teacher': rate(ok, 'teacher'),
        'review_calls_share': round(sum('review' in r['calls'] for r in ok) / max(len(ok), 1), 4),
        'mean_tokens_per_item': round(sum(r['tokens'] for r in ok) / max(len(ok), 1)),
        'wall_seconds': round(time.time() - t0),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=1), encoding='utf-8'
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
