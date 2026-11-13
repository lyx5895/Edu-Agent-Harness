"""Real API validation for the Edu vLLM deployment; does not write teaching data."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.model import ModelClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--report', default='reports/vllm-validation.json')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    report = {'base_url': base, 'checks': {}}
    started = time.monotonic()
    with httpx.Client(timeout=180, trust_env=False) as client:
        response = client.get(base+'/health')
        response.raise_for_status()
        models = client.get(base+'/v1/models')
        models.raise_for_status()
        assert any(m['id'] == 'omniedu-9b' for m in models.json()['data'])
        report['checks']['model_registration'] = True
        version = client.get(base+'/version')
        version.raise_for_status()
        report['server_version'] = version.json()

        def request(index):
            t = time.monotonic()
            result = client.post(base+'/v1/chat/completions', json={
                'model': 'omniedu-9b', 'messages': [{'role': 'user', 'content':
                    f'第{index+1}题：用一句话解释什么是分配律。'}],
                'max_tokens': 64, 'temperature': 0,
                'chat_template_kwargs': {'enable_thinking': False}})
            result.raise_for_status()
            data = result.json()
            assert data['choices'][0]['message']['content'].strip()
            return {'index': index, 'seconds': time.monotonic()-t, 'usage': data['usage']}

        batch_started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            report['concurrent_requests'] = list(pool.map(request, range(8)))
        report['batch_seconds'] = time.monotonic()-batch_started
        report['checks']['eight_concurrent_requests'] = True
        text = []
        done = False
        with client.stream('POST', base+'/v1/chat/completions', json={
                'model': 'omniedu-9b', 'messages': [{'role': 'user', 'content': '请说你好。'}],
                'stream': True, 'temperature': 0, 'max_tokens': 32,
                'chat_template_kwargs': {'enable_thinking': False}}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith('data: '):
                    continue
                payload = line[6:]
                if payload == '[DONE]':
                    done = True
                    break
                data = json.loads(payload)
                for choice in data.get('choices', []):
                    text.append(choice.get('delta', {}).get('content') or '')
        assert done and ''.join(text).strip()
        report['checks']['streaming'] = True

    model = ModelClient(base_url=base+'/v1')
    diagnosis = model.diagnose(
        {'prompt': '计算2(x+3)', 'rubric': '正确使用分配律', 'answer': '2x+6'},
        '2x+3', {'status': 'incorrect'}, {'text': '括号外的因子要乘到括号内每一项。'})
    assert diagnosis['status'] == 'incorrect'
    report['checks']['edu_structured_diagnosis'] = True
    report['diagnosis'] = diagnosis
    report['seconds'] = time.monotonic()-started
    path = ROOT/args.report
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
