"""Evaluate knowledge-point tagging against K12-KGraph exercise edges (tests_concept / tests_skill)
and K12-Bench ground_subtask1 multiple choice."""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.resources.knowledge import Knowledge, tag
from harness.llm.open_model import OpenModel

KG = ROOT / 'references/k12-kgraph'


def exercises(limit, seed):
    nodes = {n['id']: n for n in json.loads((KG / 'global_KG/nodes.json').read_text())}
    gold = {}
    for e in json.loads((KG / 'global_KG/edges.json').read_text()):
        if e['type'] in ('tests_concept', 'tests_skill'):
            gold.setdefault(e['source'], set()).add(e['target'])
    items = sorted(gold)
    random.Random(seed).shuffle(items)
    return [{'id': i, 'question': nodes[i]['name'], 'subject': i.split('_')[0], 'gold': gold[i]} for i in items[:limit]]


def score(rows, key):
    tp = sum(len(set(r[key]) & r['gold']) for r in rows)
    predicted = sum(len(r[key]) for r in rows)
    gold = sum(len(r['gold']) for r in rows)
    p, r = tp / max(predicted, 1), tp / max(gold, 1)
    return {'precision': round(p, 3), 'recall': round(r, 3), 'f1': round(2 * p * r / max(p + r, 1e-9), 3),
            'any_hit': round(sum(bool(set(x[key]) & x['gold']) for x in rows) / len(rows), 3)}


def run_tagging(model, kb, items, workers, use_stage):
    def one(item):
        stages = [kb.get(item['id'])['stage']] if use_stage else None
        bm25 = [n['id'] for n in kb.search(item['question'], item['subject'], stages, 3)]
        try:
            result = tag(model, kb, item['question'], item['subject'], stages)
        except Exception as exc:  # keep the row so failures count against the score
            result = {'ids': [], 'candidates': [], 'guesses': [], 'source': f'error:{type(exc).__name__}'}
        return {**item, 'bm25': bm25, 'tagged': result['ids'], 'pool': result['candidates'],
                'guesses': result['guesses'], 'source': result['source']}
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, items))
    summary = {'n': len(rows), 'bm25_top3': score(rows, 'bm25'), 'tagged': score(rows, 'tagged'),
               'pool_recall': score(rows, 'pool')['recall'],
               'mean_pool': round(sum(len(r['pool']) for r in rows) / len(rows), 1),
               'sources': {s: sum(r['source'] == s for r in rows) for s in {r['source'] for r in rows}}}
    return summary, rows


def run_choice(model, limit, seed, workers):
    items = [json.loads(re.sub(r'\\(.)', lambda m: m[0] if m[1] in '"\\/bfnrtu' else '\\\\' + m[1], line))
             for line in (KG / 'K12-Bench/ground_subtask1.jsonl').open()]
    random.Random(seed).shuffle(items)

    def one(item):
        options = '\n'.join(f'{k}. {v}' for k, v in item['options'].items())
        try:
            text = model.chat([{'role': 'system', 'content': model_system(model)},
                               {'role': 'user', 'content': f"{item['question']}\n{options}\n可能有多个正确选项。只输出正确选项字母，用逗号分隔。"}],
                              max_tokens=20)
        except Exception:
            text = ''
        picked = sorted({c for c in text.upper() if c in item['options']})
        return {'id': item['id'], 'answer': sorted(item['answer']), 'picked': picked}
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, items[:limit]))
    return {'n': len(rows), 'exact': round(sum(r['picked'] == r['answer'] for r in rows) / len(rows), 3)}, rows


def model_system(model):
    from harness.llm.omniedu_prompts import REGISTRY
    return REGISTRY['knowledge_point'] if model.prompt_style == 'omniedu' else '你是K12课程知识点专家。'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=300)
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--styles', default='omniedu,harness')
    p.add_argument('--out', default=str(ROOT / 'reports/knowledge-tagging.json'))
    args = p.parse_args()
    kb, items = Knowledge(), exercises(args.limit, args.seed)
    report = {'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'limit': args.limit, 'seed': args.seed, 'runs': {}}
    for style in args.styles.split(','):
        model = OpenModel()
        model.prompt_style = style
        model.logs = ROOT / 'logs/knowledge-eval-calls.jsonl'
        for use_stage in (False, True):
            name = f'{style}{"-stage" if use_stage else ""}'
            summary, rows = run_tagging(model, kb, items, args.workers, use_stage)
            report['runs'][name] = {'summary': summary, 'rows': [{**{k: v for k, v in r.items() if k != 'question'}, 'gold': sorted(r['gold'])}
                                                               for r in rows]}
            print(name, json.dumps(summary, ensure_ascii=False), flush=True)
        summary, rows = run_choice(model, args.limit, args.seed, args.workers)
        report['runs'][f'{style}-ground_subtask1'] = {'summary': summary, 'rows': rows}
        print(f'{style}-ground_subtask1', summary, flush=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
