"""Own-process-only service lifecycle; binds both APIs to loopback."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
CONFIG={'inference':('inference','inference_server:app',8000), 'harness':('harness','harness.app:app',7860)}
VLLM_MODULE='vllm.entrypoints.openai.api_server'

def identity(pid,module):
    try:
        args=Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        return module in args and Path(f'/proc/{pid}/cwd').resolve()==ROOT
    except (OSError,ValueError): return False

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['start','stop','status'])
    p.add_argument('service',choices=CONFIG)
    p.add_argument('--backend', choices=['transformers','vllm'])
    p.add_argument('--port',type=int)
    p.add_argument('--gpus',help='Comma-separated physical GPU indices')
    p.add_argument('--instance',help='Separate PID/log name for validation')
    a=p.parse_args()
    settings_path=ROOT/'service-config.json'
    settings=json.loads(settings_path.read_text()) if settings_path.exists() else {}
    backend=a.backend or settings.get('inference_backend','transformers')
    if a.instance and not all(c.isalnum() or c in '-_' for c in a.instance):
        p.error('Invalid instance name')
    if a.service!='inference' and any((a.backend,a.gpus,a.instance,a.port)):
        p.error('Overrides are only supported for inference')
    envname,module,port=CONFIG[a.service]
    if a.service=='inference' and backend=='vllm': envname,module='vllm',VLLM_MODULE
    port=a.port or port
    instance=a.instance or a.service
    logs=ROOT/'logs'; logs.mkdir(exist_ok=True)
    with (logs/f'{instance}.lock').open('w') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        pidfile=logs/f'{instance}.pid'
        pid=int(pidfile.read_text()) if pidfile.exists() else None
        accepted=[module] if a.service=='harness' else ['inference_server:app',VLLM_MODULE]
        running_module=next((m for m in accepted if pid and identity(pid,m)),None)
        alive=bool(running_module)
        if a.action=='stop':
            if alive:
                if os.getpgid(pid)!=pid: raise SystemExit('Refusing to stop an unowned process group')
                os.killpg(pid,signal.SIGTERM)
                for _ in range(60):
                    if not identity(pid,running_module): break
                    time.sleep(1)
                else: raise SystemExit('Own service did not stop within 60s; inspect logs before retrying')
            pidfile.unlink(missing_ok=True)
            print('Stopped',a.service)
            return
        if a.action=='status':
            actual_backend=('vllm' if running_module==VLLM_MODULE else 'transformers') if a.service=='inference' else None
            print(json.dumps({'service':a.service,'pid':pid,'running':alive,'backend':actual_backend if alive else backend,'url':f'http://127.0.0.1:{port}'}))
            return
        if alive:
            if running_module!=module: raise SystemExit('Another inference backend is running; stop it before switching')
            print('Already running',pid);return
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            try:s.bind(('127.0.0.1',port))
            except OSError:raise SystemExit(f'Port {port} is already occupied')
        env=os.environ.copy()
        env.update({'HF_HOME':str(ROOT/'.cache/huggingface'),'HF_HUB_OFFLINE':'1',
            'TOKENIZERS_PARALLELISM':'false','GRADIO_ANALYTICS_ENABLED':'False',
            'XDG_CACHE_HOME':str(ROOT/'.cache'),'PYTHONUNBUFFERED':'1'})
        if a.service=='inference':
            report=ROOT/'reports/model-verification.json'
            if not report.exists():raise SystemExit('Run scripts/download_model.py --verify-only first')
            checked=json.loads(report.read_text())
            if (ROOT/'models/Omni-Edu-9B/model.safetensors').stat().st_size!=checked['bytes']:
                raise SystemExit('Model file size changed; verify again')
            rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],text=True)
            used={int(r.split(',')[0]):int(r.split(',')[1]) for r in rows.strip().splitlines()}
            gpus=a.gpus or (settings.get('vllm_gpus','1') if backend=='vllm' else '1,2')
            indices=[int(i) for i in gpus.split(',')]
            if any(used.get(i,999999)>1024 for i in indices):
                raise SystemExit(f'GPU {gpus} in use; not touching other processes')
            env['CUDA_VISIBLE_DEVICES']=gpus
        command=[str(ROOT/f'.envs/{envname}/bin/python'),'-m','uvicorn',module,
                '--host','127.0.0.1','--port',str(port),'--workers','1','--timeout-graceful-shutdown','30']
        if a.service=='inference' and backend=='vllm':
            compat=settings.get('cuda_compat_path','/usr/local/cuda-13.0/compat')
            if not (Path(compat)/'libcuda.so.1').is_file(): raise SystemExit('CUDA compatibility library missing: '+compat)
            temporary=ROOT/'.cache/tmp'
            temporary.mkdir(parents=True,exist_ok=True)
            env.update({'LD_LIBRARY_PATH':compat+':'+env.get('LD_LIBRARY_PATH',''),
                'VLLM_ENABLE_CUDA_COMPATIBILITY':'1','VLLM_CUDA_COMPATIBILITY_PATH':compat,
                'VLLM_NO_USAGE_STATS':'1','DO_NOT_TRACK':'1','TMPDIR':str(temporary),
                'FLASHINFER_WORKSPACE_BASE':str(ROOT/'.cache/flashinfer'),'MAX_JOBS':'4'})
            command=[str(ROOT/'.envs/vllm/bin/python'),'-m',VLLM_MODULE,
                '--model',str(ROOT/'models/Omni-Edu-9B'),'--served-model-name','omniedu-9b',
                '--host','127.0.0.1','--port',str(port),'--dtype','bfloat16',
                '--tensor-parallel-size',str(len(indices)),'--max-model-len','4096',
                '--max-num-seqs','16','--max-num-batched-tokens','4096',
                '--gpu-memory-utilization','0.40','--limit-mm-per-prompt','{"image":1,"video":0}',
                '--gdn-prefill-backend','triton',
                '--default-chat-template-kwargs','{"enable_thinking":false}',
                '--generation-config','vllm']
        with (logs/f'{instance}.log').open('a') as log:
            proc=subprocess.Popen(command,cwd=ROOT,env=env,
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        pidfile.write_text(str(proc.pid))
        print(f'Started {instance} pid={proc.pid}; log={logs/(instance+".log")}',flush=True)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(900 if backend=='vllm' and a.service=='inference' else 300):
            if proc.poll() is not None:raise SystemExit('Service exited; inspect log')
            try:
                with opener.open(f'http://127.0.0.1:{port}/health',timeout=2) as r:
                    if (a.service=='inference' and backend=='vllm' and r.status==200) or json.load(r).get('ready'):
                        print('Ready',f'http://127.0.0.1:{port}',flush=True);return
            except Exception:pass
            time.sleep(1)
        raise SystemExit('Readiness timeout; process retained for inspection, not declared healthy')

if __name__=='__main__':main()
