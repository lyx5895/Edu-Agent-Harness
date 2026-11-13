import json
from pathlib import Path
import subprocess
import sys
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    result={}
    e2e=json.loads((ROOT/'reports/real-e2e.json').read_text())
    sid=e2e[0]['response']['session_id']
    with httpx.Client(timeout=120,trust_env=False) as client:
        before=client.get(f'http://127.0.0.1:7860/api/sessions/{sid}').json()
        for name,port in [('inference',8000),('harness',7860)]:
            old=int((ROOT/f'logs/{name}.pid').read_text())
            for action in ('stop','start'):
                subprocess.run([sys.executable,str(ROOT/'scripts/service.py'),action,name],check=True)
            new=int((ROOT/f'logs/{name}.pid').read_text())
            health=client.get(f'http://127.0.0.1:{port}/health').json()
            assert health['ready'] and old!=new
            result[name]={'old_pid':old,'new_pid':new,'health':health}
        r=client.post('http://127.0.0.1:8000/v1/chat/completions',json={
            'model':'omniedu-9b','messages':[{'role':'user','content':'一句话说明如何检查方程的解。'}],'max_tokens':64})
        r.raise_for_status()
        result['post_restart_inference']=r.json()
        assert r.json()['choices'][0]['message']['content']
        after=client.get(f'http://127.0.0.1:7860/api/sessions/{sid}').json()
        assert after==before
        result['session_restored_exactly']=True
        result['session_id']=sid
    (ROOT/'reports/restart-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print('Service restart and durable session recovery passed')


if __name__=='__main__':main()
