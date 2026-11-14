import json
import math
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return json.loads((ROOT / 'reports' / name).read_text())


def main():
    smoke = read('deployment-smoke.json')
    comparison = read('comparison.json')['rows']
    restart = read('restart-check.json')
    e2e = read('real-e2e.json')
    suites = list(ET.parse(ROOT / 'reports/unit-tests.xml').getroot().iter('testsuite'))
    unit = {key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    summary = {}
    for mode in ('direct', 'harness'):
        rows = [r for r in comparison if r['mode'] == mode]
        summary[mode] = {
            'cases': len(rows),
            'valid_output': sum(r['ok'] for r in rows),
            'correct_status': sum(r.get('status_correct', False) for r in rows),
            'correct_first_error': sum(r.get('first_error_correct', False) for r in rows),
            'exact_answer_string_leaks': sum(r.get('answer_string_leak', False) for r in rows),
            'mean_seconds': round(statistics.mean(r['seconds'] for r in rows), 3),
            'total_tokens': sum(r['usage']['total_tokens'] for r in rows),
            'monetary_cost': None,
        }
    with httpx.Client(timeout=10, trust_env=False) as c:
        health = {
            name: c.get(f'http://127.0.0.1:{port}/health').json()
            for name, port in [('inference', 8000), ('harness', 7860)]
        }
        page = c.get('http://127.0.0.1:7860/').status_code
    times = sorted(r['client_seconds'] for r in smoke if r['ok'])
    passed = len(smoke) == 20 and all(r['ok'] for r in smoke) and unit['failures'] == unit['errors'] == 0
    passed = passed and restart['session_restored_exactly'] and any(r.get('image_confirmation') for r in e2e)
    passed = passed and any(
        'teacher_report' in r and r['teacher_report']['summary'][0]['independent_check_passed'] for r in e2e
    )
    passed = passed and all(v['ready'] for v in health.values()) and page == 200
    result = {
        'at_utc': datetime.now(UTC).isoformat(),
        'acceptance_passed': passed,
        'model': read('model-verification.json'),
        'environment': read('environment.json'),
        'unit_tests': unit,
        'smoke': {
            'requests': len(smoke),
            'passed': sum(r['ok'] for r in smoke),
            'mean_seconds': round(statistics.mean(times), 3),
            'p95_seconds': round(times[math.ceil(0.95 * len(times)) - 1], 3),
            'max_peak_allocated_gib_per_gpu': [
                max(r['metrics']['peak_allocated_gib'][i] for r in smoke if r['ok']) for i in (0, 1)
            ],
        },
        'real_text_and_image_e2e': True,
        'restart_and_persistence': restart['session_restored_exactly'],
        'comparison': summary,
        'services': health,
        'page_http_status': page,
        'gpu_snapshot': subprocess.check_output(
            ['nvidia-smi', '--query-gpu=index,memory.used', '--format=csv'], text=True
        ),
        'limitations': [
            'Personal local demo; no real user authorization boundary.',
            'Transformers fallback; current vLLM dependency preflight only, no vLLM runtime test.',
            'Six authored comparison cases, automatic proxies, no learning-gain validation or human blind rating.',
            'No monetary cost estimate without electricity and hardware accounting.',
            'Two holdout items per skill per learner; expand bank for additional independent reviews.',
            'Image smoke tests use synthetic printed homework, not a handwriting benchmark.',
        ],
    }
    (ROOT / 'reports/acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not passed:
        sys.exit(1)


if __name__ == '__main__':
    main()
