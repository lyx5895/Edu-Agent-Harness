"""Own-process-only service lifecycle; binds both APIs to loopback."""

import argparse
import fcntl
import http.client
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    'inference': ('inference', 'inference_server:app', 8000),
    'harness': ('harness', 'harness.app:app', 7860),
    'judge': ('vllm', 'vllm.entrypoints.openai.api_server', 8002),
    'baseline': ('vllm', 'vllm.entrypoints.openai.api_server', 8003),
}
# Evaluation-only models; never used by the teaching app.
AUX_DEFAULTS = {
    'judge': {
        'model': '/default-filesys/workspace/models/Qwen3.5-27B',
        'served_model_name': 'qwen3.5-27b',
        'gpus': '3',
        'gpu_memory_utilization': 0.85,
    },
    'baseline': {
        'model': '/default-filesys/workspace/models/Qwen3.5-9B',
        'served_model_name': 'qwen3.5-9b',
        'gpus': '4',
        'gpu_memory_utilization': 0.6,
    },
}
VLLM_MODULE = 'vllm.entrypoints.openai.api_server'
VLLM_DEFAULTS = {
    'max_model_len': 32768,
    'max_num_seqs': 32,
    'max_num_batched_tokens': 8192,
    'gpu_memory_utilization': 0.6,
}


def warmup(port, model='omniedu-9b'):
    """Exercise text, image, long-prompt, batched and JSON-schema paths so Triton
    kernels compile before real students hit them."""
    import base64
    import io
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(body):
        data = json.dumps(
            {
                'model': model,
                'temperature': 0,
                'max_tokens': 16,
                'chat_template_kwargs': {'enable_thinking': False},
                **body,
            }
        ).encode()
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/v1/chat/completions', data, {'Content-Type': 'application/json'}
        )
        with opener.open(req, timeout=300) as r:
            r.read()

    buf = io.BytesIO()
    Image.new('RGB', (1024, 768), 'white').save(buf, format='PNG')
    image = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
    requests = [
        {'messages': [{'role': 'user', 'content': '你好'}]},
        {
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': '描述图片'},
                        {'type': 'image_url', 'image_url': {'url': image}},
                    ],
                }
            ]
        },
        {'messages': [{'role': 'user', 'content': '材料：' + '学习' * 3000 + '\n请概括。'}]},
        {
            'messages': [{'role': 'user', 'content': '回答ok'}],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'w', 'schema': schema}},
        },
    ]
    for body in requests:
        post(body)
    with ThreadPoolExecutor(8) as pool:
        list(
            pool.map(
                post, [{'messages': [{'role': 'user', 'content': f'第{i}题'}], 'max_tokens': 32} for i in range(8)]
            )
        )


def identity(pid, module):
    try:
        args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        return module in args and Path(f'/proc/{pid}/cwd').resolve() == ROOT
    except (OSError, ValueError):
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['start', 'stop', 'status'])
    p.add_argument('service', choices=CONFIG)
    p.add_argument('--backend', choices=['transformers', 'vllm'])
    p.add_argument('--port', type=int)
    p.add_argument('--gpus', help='Comma-separated physical GPU indices')
    p.add_argument('--instance', help='Separate PID/log name for validation')
    a = p.parse_args()
    settings_path = ROOT / 'service-config.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    backend = a.backend or settings.get('inference_backend', 'transformers')
    if a.instance and not all(c.isalnum() or c in '-_' for c in a.instance):
        p.error('Invalid instance name')
    aux = (
        {**AUX_DEFAULTS[a.service], **settings.get('aux_models', {}).get(a.service, {})}
        if a.service in AUX_DEFAULTS
        else None
    )
    if a.service == 'harness' and any((a.backend, a.gpus, a.instance, a.port)):
        p.error('Overrides are not supported for harness')
    envname, module, port = CONFIG[a.service]
    if a.service == 'inference' and backend == 'vllm':
        envname, module = 'vllm', VLLM_MODULE
    is_vllm = module == VLLM_MODULE
    port = a.port or (aux or {}).get('port') or port
    instance = a.instance or a.service
    logs = ROOT / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / f'{instance}.lock').open('w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        pidfile = logs / f'{instance}.pid'
        pid = int(pidfile.read_text()) if pidfile.exists() else None
        accepted = [module] if a.service == 'harness' or aux else ['inference_server:app', VLLM_MODULE]
        running_module = next((m for m in accepted if pid and identity(pid, m)), None)
        alive = bool(running_module)
        if a.action == 'stop':
            if alive:
                if os.getpgid(pid) != pid:
                    raise SystemExit('Refusing to stop an unowned process group')
                os.killpg(pid, signal.SIGTERM)
                for _ in range(60):
                    if not identity(pid, running_module):
                        break
                    time.sleep(1)
                else:
                    raise SystemExit('Own service did not stop within 60s; inspect logs before retrying')
            pidfile.unlink(missing_ok=True)
            print('Stopped', a.service)
            return
        if a.action == 'status':
            actual_backend = (
                ('vllm' if running_module == VLLM_MODULE else 'transformers')
                if a.service == 'inference'
                else 'vllm'
                if aux
                else None
            )
            print(
                json.dumps(
                    {
                        'service': a.service,
                        'pid': pid,
                        'running': alive,
                        'backend': actual_backend if alive else backend if a.service == 'inference' else actual_backend,
                        'model': aux['served_model_name'] if aux else None,
                        'url': f'http://127.0.0.1:{port}',
                    }
                )
            )
            return
        if alive:
            if running_module != module:
                raise SystemExit('Another inference backend is running; stop it before switching')
            print('Already running', pid)
            return
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
            except OSError:
                raise SystemExit(f'Port {port} is already occupied') from None
        env = os.environ.copy()
        env.update(
            {
                'HF_HOME': str(ROOT / '.cache/huggingface'),
                'HF_HUB_OFFLINE': '1',
                'TOKENIZERS_PARALLELISM': 'false',
                'GRADIO_ANALYTICS_ENABLED': 'False',
                'XDG_CACHE_HOME': str(ROOT / '.cache'),
                'PYTHONUNBUFFERED': '1',
            }
        )
        if a.service == 'inference':
            report = ROOT / 'reports/model-verification.json'
            if not report.exists():
                raise SystemExit('Run scripts/download_model.py --verify-only first')
            checked = json.loads(report.read_text())
            if (ROOT / 'models/Omni-Edu-9B/model.safetensors').stat().st_size != checked['bytes']:
                raise SystemExit('Model file size changed; verify again')
        if a.service == 'inference' or aux:
            rows = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=index,memory.used', '--format=csv,noheader,nounits'], text=True
            )
            used = {int(r.split(',')[0]): int(r.split(',')[1]) for r in rows.strip().splitlines()}
            gpus = a.gpus or (aux['gpus'] if aux else settings.get('vllm_gpus', '1') if backend == 'vllm' else '1,2')
            indices = [int(i) for i in gpus.split(',')]
            if any(used.get(i, 999999) > 1024 for i in indices):
                raise SystemExit(f'GPU {gpus} in use; not touching other processes')
            env['CUDA_VISIBLE_DEVICES'] = gpus
        command = [
            str(ROOT / f'.envs/{envname}/bin/python'),
            '-m',
            'uvicorn',
            module,
            '--host',
            '127.0.0.1',
            '--port',
            str(port),
            '--workers',
            '1',
            '--timeout-graceful-shutdown',
            '30',
        ]
        if is_vllm:
            compat = settings.get('cuda_compat_path', '/usr/local/cuda-13.0/compat')
            if not (Path(compat) / 'libcuda.so.1').is_file():
                raise SystemExit('CUDA compatibility library missing: ' + compat)
            temporary = ROOT / '.cache/tmp'
            temporary.mkdir(parents=True, exist_ok=True)
            env.update(
                {
                    'LD_LIBRARY_PATH': compat + ':' + env.get('LD_LIBRARY_PATH', ''),
                    'VLLM_ENABLE_CUDA_COMPATIBILITY': '1',
                    'VLLM_CUDA_COMPATIBILITY_PATH': compat,
                    'VLLM_NO_USAGE_STATS': '1',
                    'DO_NOT_TRACK': '1',
                    'TMPDIR': str(temporary),
                    'FLASHINFER_WORKSPACE_BASE': str(ROOT / '.cache/flashinfer'),
                    'MAX_JOBS': '4',
                }
            )
            vllm = {**VLLM_DEFAULTS, **settings.get('vllm', {}), **(aux or {})}
            served = aux['served_model_name'] if aux else 'omniedu-9b'
            command = [
                str(ROOT / '.envs/vllm/bin/python'),
                '-m',
                VLLM_MODULE,
                '--model',
                aux['model'] if aux else str(ROOT / 'models/Omni-Edu-9B'),
                '--served-model-name',
                served,
                '--host',
                '127.0.0.1',
                '--port',
                str(port),
                '--dtype',
                'bfloat16',
                '--tensor-parallel-size',
                str(len(indices)),
                '--max-model-len',
                str(vllm['max_model_len']),
                '--max-num-seqs',
                str(vllm['max_num_seqs']),
                '--max-num-batched-tokens',
                str(vllm['max_num_batched_tokens']),
                '--gpu-memory-utilization',
                str(vllm['gpu_memory_utilization']),
                '--limit-mm-per-prompt',
                '{"image":1,"video":0}',
                '--gdn-prefill-backend',
                'triton',
                '--default-chat-template-kwargs',
                '{"enable_thinking":false}',
                '--generation-config',
                'vllm',
            ]
        with (logs / f'{instance}.log').open('a') as log:
            proc = subprocess.Popen(
                command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
            )
        pidfile.write_text(str(proc.pid))
        print(f'Started {instance} pid={proc.pid}; log={logs / (instance + ".log")}', flush=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(900 if is_vllm else 300):
            if proc.poll() is not None:
                raise SystemExit('Service exited; inspect log')
            try:
                with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as r:
                    up = (is_vllm and r.status == 200) or json.load(r).get('ready')
            except (OSError, ValueError, http.client.HTTPException):
                up = False
            if up:
                if is_vllm:
                    print('Warming up kernels', flush=True)
                    try:
                        warmup(port, served)
                    except Exception as e:
                        raise SystemExit(
                            f'Warmup failed ({e}); process retained for inspection, not declared healthy'
                        ) from e
                print('Ready', f'http://127.0.0.1:{port}', flush=True)
                return
            time.sleep(1)
        raise SystemExit('Readiness timeout; process retained for inspection, not declared healthy')


if __name__ == '__main__':
    main()
