import hashlib
import json
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / '.cache/evalsrc'
REPORTS = ROOT / 'reports/benchmark'

ENDPOINTS = {
    'omniedu': ('http://127.0.0.1:8000/v1', 'omniedu-9b'),
    'baseline': ('http://127.0.0.1:8003/v1', 'qwen3.5-9b'),
    'judge': ('http://127.0.0.1:8002/v1', 'qwen3.5-27b'),
}


def chat(endpoint, messages, max_tokens=512, temperature=0, response_format=None, retries=2):
    base, model = ENDPOINTS[endpoint]
    body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature,
            'chat_template_kwargs': {'enable_thinking': False}}
    if response_format:
        body['response_format'] = response_format
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=300, trust_env=False) as client:
                r = client.post(base + '/chat/completions', json=body)
                r.raise_for_status()
                return r.json()['choices'][0]['message']['content'] or ''
        except (httpx.HTTPError, KeyError) as e:
            if attempt == retries:
                raise RuntimeError(f'{endpoint} call failed: {e}') from e
            time.sleep(2 * (attempt + 1))


def schema_format(name, schema):
    return {'type': 'json_schema', 'json_schema': {'name': name, 'schema': schema}}


def chat_json(endpoint, messages, name, schema, max_tokens, temperature=0, attempts=3):
    # Constrained decoding can still run away in whitespace until max_tokens; resample instead of failing the item.
    for attempt in range(attempts):
        raw = chat(endpoint, messages, max_tokens=max_tokens, temperature=temperature if attempt == 0 else max(temperature, 0.6),
                   response_format=schema_format(name, schema))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt == attempts - 1:
                raise


def parallel(fn, items, workers=16):
    def safe(item):
        try:
            return fn(item)
        except Exception as e:  # keep the run going; failures are reported per item
            return {'error': f'{type(e).__name__}: {e}'}
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(safe, items))


def sample(rows, limit, seed=7):
    rows = list(rows)
    if limit and limit < len(rows):
        rows = random.Random(seed).sample(rows, limit)
    return rows


def provenance():
    repos = {}
    for repo in ('UnifyingAITutorEvaluation', 'mathtutorbench'):
        try:
            repos[repo] = subprocess.check_output(['git', '-C', str(SOURCES / repo), 'rev-parse', 'HEAD'], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            repos[repo] = None
    step = SOURCES / 'stepverify/stepverify.json'
    repos['stepverify_sha256'] = hashlib.sha256(step.read_bytes()).hexdigest() if step.exists() else None
    return repos


def final_number(text):
    numbers = re.findall(r'-?\d[\d,]*\.?\d*', text or '')
    return normalize_number(numbers[-1]) if numbers else None


def normalize_number(value):
    value = str(value).replace(',', '').strip().rstrip('.')
    try:
        number = float(value)
    except ValueError:
        return None
    return str(int(number)) if number == int(number) else f'{number:g}'


def mentions_number(text, number):
    if number is None:
        return False
    candidates = {normalize_number(n) for n in re.findall(r'-?\d[\d,]*\.?\d*', text or '')}
    return number in candidates


def write_report(name, report):
    REPORTS.mkdir(parents=True, exist_ok=True)
    report['generated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    report['sources'] = provenance()
    path = REPORTS / f'{name}.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return path
