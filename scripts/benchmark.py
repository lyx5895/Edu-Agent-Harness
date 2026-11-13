"""Comparative benchmark: raw Qwen3.5-9B vs raw OmniEdu-9B vs OmniEdu-9B + harness.

Tracks:
  calibration  judge (Qwen3.5-27B) vs MRBench human labels
  correctness  StepVerify: is the student's solution wrong? (ground truth, no judge)
  location     StepVerify: first erroneous step (raw models only; harness has no step output)
  response     MRBench dialogues: next tutor turn, judged on the 8 MRBench dimensions
  dialogue     multi-turn tutoring of simulated students seeded with real errors
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.common import ROOT, parallel, sample, write_report
from bench.data import mrbench, stepverify, stepverify_problems
from bench.judge import DIMENSIONS, agreement, judge, lenient
from bench.simstudent import run_dialogue, submitted_gold
from bench.systems import HarnessTutor, RawTutor


def systems(names):
    table = {'raw-baseline': lambda: RawTutor('baseline'), 'raw-omniedu': lambda: RawTutor('omniedu'),
             'raw-baseline-ref': lambda: RawTutor('baseline', True), 'raw-omniedu-ref': lambda: RawTutor('omniedu', True),
             'harness-omniedu': HarnessTutor, 'harness-omniedu-v4': lambda: HarnessTutor('open-task-policy-v4'),
             'harness-omniedu-v5': lambda: HarnessTutor('open-task-policy-v5'),
             'harness-omniedu-xr': lambda: HarnessTutor(cross_review=True),
             'harness-omniedu-v5-xr': lambda: HarnessTutor('open-task-policy-v5', cross_review=True)}
    return [table[n]() for n in names]


def rate(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def track_calibration(args):
    records = []
    for conv in sample(mrbench(), args.limit):
        for tutor, item in conv['anno_llm_responses'].items():
            records.append({'conversation_id': conv['conversation_id'], 'tutor': tutor, 'response': item['response'],
                            'human': item['annotation'], 'history': conv['conversation_history'], 'gold': conv.get('Ground_Truth_Solution', '')})
    out = parallel(lambda r: {'judge': judge(r['history'], r['gold'], r['response'])}, records, args.workers)
    for r, o in zip(records, out):
        r.update(o)
        r.pop('history'); r.pop('gold')
    per_tutor = {t: {d: rate([lenient(d, r['judge'][d]) in ('Yes', 'OK') for r in records if r['tutor'] == t and 'judge' in r])
                     for d in DIMENSIONS} for t in sorted({r['tutor'] for r in records})}
    return {'agreement': agreement(records), 'judge_positive_rate_by_tutor': per_tutor,
            'errors': sum('error' in r for r in records), 'records': records}


def track_correctness(args):
    problems = sample(sorted({x['problem_id'] for x in stepverify()}), args.limit)
    keep = set(problems)
    return run_correctness([x for x in stepverify() if x['problem_id'] in keep], args)


def track_correctness_zh(args):
    return run_correctness(derived('stepverify-zh'), args)


def track_correctness_multi(args):
    report = run_correctness(derived('multisubject-zh'), args)
    for name, rows in report['records'].items():
        subjects = sorted({r['id'].split('-')[0] for r in rows})
        report['summary'][name]['accuracy_by_subject'] = {
            s: rate([r['flagged_error'] == r['is_error'] for r in rows if r['id'].startswith(s) and 'error' not in r]) for s in subjects}
    return report


def derived(name):
    path = ROOT / 'bench/data' / f'{name}.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()]


def run_correctness(items, args):
    problems = {x['problem_id'] for x in items}
    summary, records = {}, {}
    for system in systems(args.systems):
        out = parallel(system.judge_correctness, items, args.workers)
        rows = [{'id': i['id'], 'is_error': i['is_error'], **o} for i, o in zip(items, out)]
        ok = [r for r in rows if 'error' not in r]
        wrong = [r for r in ok if r['is_error']]
        right = [r for r in ok if not r['is_error']]
        tp = sum(r['flagged_error'] for r in wrong)
        fp = sum(r['flagged_error'] for r in right)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / len(wrong) if wrong else 0
        summary[system.name] = {
            'n': len(ok), 'failed_calls': len(rows) - len(ok),
            'accuracy': rate([r['flagged_error'] == r['is_error'] for r in ok]),
            'error_f1': round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0,
            'false_acceptance_rate': rate([not r['flagged_error'] for r in wrong]),
            'false_rejection_rate': rate([r['flagged_error'] for r in right]),
            'abstain_rate': rate([r.get('abstained', False) for r in ok]),
            'status_counts': dict(Counter(r['status'] for r in ok)),
        }
        reviewed = [r for r in ok if r.get('cross_review')]
        if reviewed:
            summary[system.name]['cross_review'] = {
                'reviewed': len(reviewed), 'agreed': sum(r['cross_review']['agreed'] for r in reviewed),
                'overturned': sum(r['cross_review']['overturned'] for r in reviewed),
                'overturned_on_correct_items': sum(r['cross_review']['overturned'] and not r['is_error'] for r in reviewed),
                'overturned_on_error_items': sum(r['cross_review']['overturned'] and r['is_error'] for r in reviewed)}
        records[system.name] = rows
    return {'problems': len(problems), 'items': len(items), 'summary': summary, 'records': records,
            'note': 'harness "unknown" counts as flagged (routed to teacher), reported separately as abstain_rate'}


def track_location(args):
    items = sample([x for x in stepverify() if x['is_error']], args.limit)
    summary, records = {}, {}
    for system in systems([n for n in args.systems if n.startswith('raw')]):
        out = parallel(system.locate, items, args.workers)
        rows = [{'id': i['id'], 'error_step': i['error_step'], **o} for i, o in zip(items, out)]
        summary[system.name] = {'n': len(rows), 'accuracy': rate([r.get('step') == r['error_step'] for r in rows if 'error' not in r])}
        records[system.name] = rows
    return {'summary': summary, 'records': records}


def track_response(args):
    convs = sample([c for c in mrbench() if c['question'] and c['gold_answer']], args.limit)
    summary, records = {}, {}
    for system in systems(args.systems):
        generated = parallel(system.respond, convs, args.workers)
        judged = parallel(lambda pair: {'judge': judge(pair[0]['conversation_history'], pair[0]['Ground_Truth_Solution'], pair[1]['response'])}
                          if 'response' in pair[1] else {}, list(zip(convs, generated)), args.workers)
        rows = [{'conversation_id': c['conversation_id'], **g, **j} for c, g, j in zip(convs, generated, judged)]
        ok = [r for r in rows if 'judge' in r]
        summary[system.name] = {'n': len(ok), 'failed': len(rows) - len(ok),
            **{d: rate([lenient(d, r['judge'][d]) in ('Yes', 'OK') for r in ok]) for d in DIMENSIONS},
            'reveals_answer_exact': rate([r['judge']['Revealing_of_the_Answer'].startswith('Yes') for r in ok]),
            'actions': dict(Counter(r.get('action') for r in ok if r.get('action')))}
        records[system.name] = rows
    return {'conversations': len(convs), 'summary': summary, 'records': records,
            'note': 'lenient rates: share of responses judged Yes/To some extent (Revealing: any Yes; Tone: not offensive)'}


def track_dialogue(args):
    problems = sample(stepverify_problems(), args.limit)
    summary, records = {}, {}
    for system in systems(args.systems):
        rows = parallel(lambda p: {'id': p['id'], **run_dialogue(system, p, args.max_turns, args.persona)}, problems, args.workers)
        summary[system.name] = summarize_dialogue(rows)
        records[system.name] = rows
    return {'problems': len(problems), 'max_turns': args.max_turns, 'persona': args.persona, 'summary': summary, 'records': records,
            'note': 'leak = tutor states the gold final answer before the student does; simulator is Qwen3.5-27B'}


def summarize_dialogue(rows):
    ok = [r for r in rows if 'error' not in r]
    solved = [r for r in ok if r['solved']]
    return {
        'n': len(ok), 'failed': len(rows) - len(ok),
        'solved_rate': rate([r['solved'] for r in ok]),
        'solved_without_leak_rate': rate([r['solved'] and not r.get('solved_after_leak') for r in ok]),
        'answer_leak_rate': rate([r['leaked_turn'] is not None for r in ok]),
        'mean_tutor_turns_when_solved': round(sum(r['turns'] for r in solved) / len(solved), 2) if solved else None,
        'escalated_to_teacher_rate': rate([r.get('escalated', False) for r in ok]),
    }


def rescore_dialogue(path):
    """Re-derive solved/turns from saved transcripts with the current answer-matching rule."""
    report = json.loads(Path(path).read_text())
    gold = {p['id']: p['gold_answer'] for p in stepverify_problems()}
    for name, rows in report['records'].items():
        for r in rows:
            if 'error' in r:
                continue
            tutor_turns, r['solved'] = 0, False
            for t in r['transcript'][1:]:
                if t['speaker'] == 'tutor':
                    tutor_turns += 1
                elif submitted_gold(t, gold[r['id']]):
                    r.update(solved=True, turns=tutor_turns)
                    break
            if r['solved']:
                r['solved_after_leak'] = r['leaked_turn'] is not None and r['leaked_turn'] <= r['turns']
                r['leaked_turn'] = r['leaked_turn'] if r['solved_after_leak'] else None
        report['summary'][name] = summarize_dialogue(rows)
    report['rescored'] = 'solved also accepts the last number in the student message (simulator final_answer field is unreliable)'
    return report


TRACKS = {'calibration': track_calibration, 'correctness': track_correctness, 'correctness-zh': track_correctness_zh,
          'correctness-multi': track_correctness_multi, 'location': track_location,
          'response': track_response, 'dialogue': track_dialogue}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('tracks', nargs='*', help=f'One or more of: {", ".join(TRACKS)}')
    p.add_argument('--limit', type=int, default=None, help='Sample size (problems/conversations); default: track default')
    p.add_argument('--systems', default='raw-baseline,raw-omniedu,harness-omniedu')
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--max-turns', type=int, default=6)
    p.add_argument('--persona', default='cooperative', choices=['cooperative', 'persistent', 'gamer', 'disengaged'])
    p.add_argument('--tag', default='')
    p.add_argument('--rescore', help='Rescore a saved dialogue report instead of running tracks')
    args = p.parse_args()
    if args.rescore:
        report = rescore_dialogue(args.rescore)
        path = write_report(Path(args.rescore).stem + '-rescored', report)
        print(f'== rescored -> {path}')
        for row, values in report['summary'].items():
            print(f'  {row}: {values}')
        return
    if not args.tracks or set(args.tracks) - set(TRACKS):
        p.error(f'tracks must be one or more of: {", ".join(TRACKS)}')
    args.systems = args.systems.split(',')
    defaults = {'calibration': None, 'correctness': 200, 'correctness-zh': None, 'correctness-multi': None,
                'location': 300, 'response': None, 'dialogue': 60}
    limit = args.limit
    for name in args.tracks:
        args.limit = limit if limit is not None else defaults[name]
        started = time.time()
        report = TRACKS[name](args)
        report.update(track=name, limit=args.limit, systems=args.systems, seconds=round(time.time() - started, 1))
        path = write_report(name + (f'-{args.tag}' if args.tag else ''), report)
        print(f'== {name} ({report["seconds"]}s) -> {path}')
        for key in ('agreement', 'summary'):
            if key in report:
                for row, values in report[key].items():
                    print(f'  {row}: {values}')


if __name__ == '__main__':
    main()
