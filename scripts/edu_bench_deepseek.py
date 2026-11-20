"""Speed test of the DeepSeek official API on the workbench's real requests, optionally side by side with the gateway.

Replays recorded requests (scripts/edu_bench_requests.json: one question generation, one question review) and
measures, per call: time to the first token, total time, output tokens, output tokens per second, reasoning tokens,
and whether the reply is usable (valid JSON that passes the same pydantic schema the workbench uses).

Key: DEEPSEEK_API_KEY in the environment, else a line DEEPSEEK_API_KEY=... in .envs/deepseek.env (outside the repo,
like .envs/edu.env). The key is never printed.

    .envs/harness/bin/python scripts/edu_bench_deepseek.py --list-models
    .envs/harness/bin/python scripts/edu_bench_deepseek.py                       # deepseek-chat, 3 runs per request
    .envs/harness/bin/python scripts/edu_bench_deepseek.py --model deepseek-reasoner --runs 5
    .envs/harness/bin/python scripts/edu_bench_deepseek.py --compare-gateway     # same requests on EDU_LLM_BASE_URL
    .envs/harness/bin/python scripts/edu_bench_deepseek.py --task generate_questions --concurrency 3

The official API takes response_format json_object (not the gateway's json_schema); --format switches it. With
json_object (or none) the pydantic schema is appended to the system prompt, since that is the only place the model
learns the field names; "可用" then says whether that is enough.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edu_core.llm import parse_json, strict_schema  # noqa: E402
from edu_core.workflow.schemas import QualityOutput, QuestionSet  # noqa: E402

SCHEMAS = {'generate_questions': QuestionSet, 'review_question': QualityOutput}
LABELS = {'generate_questions': '出题', 'review_question': '题目审核'}


def read_key(env_name, env_file):
    if os.environ.get(env_name):
        return os.environ[env_name]
    path = ROOT / env_file
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            name, _, value = line.strip().partition('=')
            if name == env_name and value.strip():
                return value.strip().strip('"\'')
    sys.exit(f'没有找到 {env_name}：设置环境变量，或在 {env_file} 里写一行 {env_name}=...')


class Endpoint:
    def __init__(self, name, base_url, key, model, fmt, max_tokens, timeout):
        self.name, self.model, self.fmt, self.max_tokens = name, model, fmt, max_tokens
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.client = httpx.Client(timeout=timeout, headers={'Authorization': f'Bearer {key}'})

    def body(self, case):
        schema = SCHEMAS[case['task']]
        system = case['system']
        if self.fmt in ('json_object', 'none'):
            # Without json_schema the model is never told the field names; the workbench prompts rely on the schema
            # for them, so here the schema goes into the prompt (what the workbench would have to do on this API).
            system += '\n输出的 JSON 必须符合下面的 JSON Schema（字段名、类型都要一致）：\n' + json.dumps(
                strict_schema(schema), ensure_ascii=False
            )
        body = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': case['user']}],
            'max_tokens': self.max_tokens,
            'temperature': 0,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        if self.fmt == 'json_object':
            body['response_format'] = {'type': 'json_object'}
        elif self.fmt == 'json_schema':
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': schema.__name__, 'strict': True, 'schema': strict_schema(schema)},
            }
        return body

    def call(self, case):
        """One streamed request. First token = first content or reasoning delta."""
        t0 = time.time()
        first, content, reasoning_chars, usage, finish = None, [], 0, {}, None
        try:
            with self.client.stream('POST', self.url, json=self.body(case)) as r:
                if r.status_code != 200:
                    return {'ok': False, 'seconds': round(time.time() - t0, 1), 'error': f'HTTP {r.status_code}: {r.read()[:200]!r}'}
                for line in r.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    chunk = json.loads(data)
                    usage = chunk.get('usage') or usage
                    for choice in chunk.get('choices') or []:
                        delta = choice.get('delta') or {}
                        piece, think = delta.get('content') or '', delta.get('reasoning_content') or ''
                        if (piece or think) and first is None:
                            first = time.time() - t0
                        content.append(piece)
                        reasoning_chars += len(think)
                        finish = choice.get('finish_reason') or finish
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            return {'ok': False, 'seconds': round(time.time() - t0, 1), 'error': f'{type(e).__name__}: {e}'[:200]}
        total = time.time() - t0
        out_tokens = usage.get('completion_tokens')
        details = usage.get('completion_tokens_details') or {}
        row = {
            'ok': True,
            'seconds': round(total, 1),
            'first_token': round(first, 1) if first is not None else None,
            'out_tokens': out_tokens,
            'reasoning_tokens': details.get('reasoning_tokens') or (None if not reasoning_chars else f'~{reasoning_chars} 字'),
            'tok_per_s': round(out_tokens / (total - (first or 0)), 1) if out_tokens and total > (first or 0) else None,
            'finish': finish,
        }
        try:
            SCHEMAS[case['task']].model_validate(parse_json(''.join(content)))
            row['usable'] = True
        except Exception as e:  # noqa: BLE001 - reported, not raised
            row['usable'] = False
            row['why_unusable'] = ('输出被截断' if finish == 'length' else str(e))[:120]
        return row


def summarize(name, task, rows):
    ok = [r for r in rows if r['ok']]
    line = f'{name:<10} {LABELS[task]:<5} 成功 {len(ok)}/{len(rows)}'
    if ok:
        secs = sorted(r['seconds'] for r in ok)
        firsts = [r['first_token'] for r in ok if r['first_token'] is not None]
        speeds = [r['tok_per_s'] for r in ok if r['tok_per_s']]
        tokens = [r['out_tokens'] for r in ok if r['out_tokens']]
        line += (
            f' | 总用时 中位 {statistics.median(secs):.1f}s 最慢 {secs[-1]:.1f}s'
            f' | 首 token {statistics.median(firsts):.1f}s' * bool(firsts)
            + f' | 输出 {statistics.median(tokens):.0f} tok' * bool(tokens)
            + f' | {statistics.median(speeds):.0f} tok/s' * bool(speeds)
            + f' | 可用 {sum(1 for r in ok if r.get("usable"))}/{len(ok)}'
        )
    return line


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--base-url', default='https://api.deepseek.com')
    ap.add_argument('--model', default='deepseek-chat')
    ap.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    ap.add_argument('--key-file', default='.envs/deepseek.env')
    ap.add_argument('--format', choices=['json_object', 'json_schema', 'none'], default='json_object')
    ap.add_argument('--requests', default=str(ROOT / 'scripts/edu_bench_requests.json'))
    ap.add_argument('--task', choices=sorted(SCHEMAS), help='only this request (default: all)')
    ap.add_argument('--runs', type=int, default=3, help='calls per request and endpoint')
    ap.add_argument('--concurrency', type=int, default=1, help='calls in flight per endpoint')
    ap.add_argument('--max-tokens', type=int, default=6000)
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--compare-gateway', action='store_true', help='also run on EDU_LLM_BASE_URL with EDU_LLM_API_KEY')
    ap.add_argument('--gateway-model', default='DeepSeek-V4.1-Flash')
    ap.add_argument('--list-models', action='store_true')
    ap.add_argument('--out', help='write every call as JSON here')
    args = ap.parse_args()

    key = read_key(args.key_env, args.key_file)
    if args.list_models:
        r = httpx.get(args.base_url.rstrip('/') + '/models', headers={'Authorization': f'Bearer {key}'}, timeout=30)
        r.raise_for_status()
        print('\n'.join(sorted(m['id'] for m in r.json().get('data', []))))
        return

    cases = [c for c in json.load(open(args.requests, encoding='utf-8'))['cases'] if not args.task or c['task'] == args.task]
    endpoints = [Endpoint('官方 API', args.base_url, key, args.model, args.format, args.max_tokens, args.timeout)]
    if args.compare_gateway:
        gw_key = read_key('EDU_LLM_API_KEY', '.envs/edu.env')
        gw_url = os.environ.get('EDU_LLM_BASE_URL', 'https://lightingtheword.com/v1')
        # The gateway is what the workbench uses today: same structured-output mode as the workbench.
        endpoints.append(Endpoint('网关', gw_url, gw_key, args.gateway_model, 'json_schema', args.max_tokens, args.timeout))

    print(f'官方 API：{args.base_url} · {args.model} · {args.format}'
          + (f'\n网关：{endpoints[-1].url.rsplit("/chat", 1)[0]} · {args.gateway_model} · json_schema' if args.compare_gateway else ''))
    print(f'每个请求每端 {args.runs} 次，并发 {args.concurrency}\n')
    results = []
    for case in cases:
        print(f'— {LABELS[case["task"]]}（记录时网关用时 {case["gateway_seconds"]}s）')
        for ep in endpoints:
            with ThreadPoolExecutor(max(1, args.concurrency)) as pool:
                rows = list(pool.map(lambda _i, ep=ep: ep.call(case), range(args.runs)))
            for i, r in enumerate(rows, 1):
                detail = (f'{r["seconds"]}s，首 token {r["first_token"]}s，{r["out_tokens"]} tok，{r["tok_per_s"]} tok/s'
                          + (f'，推理 {r["reasoning_tokens"]}' if r.get('reasoning_tokens') else '')
                          + ('' if r.get('usable') else f'，不可用：{r.get("why_unusable")}')) if r['ok'] else f'失败：{r["error"]}'
                print(f'    {ep.name} 第 {i} 次：{detail}')
                results.append({'endpoint': ep.name, 'model': ep.model, 'task': case['task'], 'run': i, **r})
            print('  ' + summarize(ep.name, case['task'], rows))
        print()
    if args.out:
        json.dump(results, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'逐次结果已写入 {args.out}')


if __name__ == '__main__':
    main()
