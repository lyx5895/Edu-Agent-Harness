"""Real image confirmation and pending-question recovery across app restart."""

import json
import subprocess
import uuid
from pathlib import Path

import httpx
from smoke_inference import image_url

ROOT = Path(__file__).resolve().parents[1]


def main():
    result = {}
    with httpx.Client(base_url='http://127.0.0.1:7860', timeout=600, trust_env=False) as c:

        def post(path, data):
            r = c.post(path, json=data)
            r.raise_for_status()
            return r.json()

        s = post('/api/sessions', {'learner': 'runtime-image-' + uuid.uuid4().hex[:8], 'skill': 'k01'})
        sid = s['session_id']
        path = f'/api/sessions/{sid}'
        image = post(path + '/image', {'request_id': 'image', 'image_url': image_url()})
        assert image['transcription']['text']
        pending = post(path + '/confirm', {'request_id': 'confirm', 'text': '2x+3'})
        assert pending['teaching_action'] == 'probe'
        before = c.get(path).json()
        for action in ('stop', 'start'):
            subprocess.run(
                [str(ROOT / '.envs/harness/bin/python'), str(ROOT / 'scripts/service.py'), action, 'harness'],
                check=True,
                cwd=ROOT,
            )
        after = c.get(path).json()
        assert after == before
        response = post(path + '/reply', {'request_id': 'reply', 'text': '我知道每项都要乘，只是漏写了。'})
        assert response['evidence'] == 'dialogue_only'
        replay = c.get(f'/api/teacher/sessions/{sid}/replay').json()
        assert replay['passed']
        result = {
            'passed': True,
            'session_id': sid,
            'image_transcription': image['transcription'],
            'pending_question': before['pending_question'],
            'identical_after_restart': after == before,
            'continued_reply_action': response['teaching_action'],
            'replay': replay,
        }
    (ROOT / 'reports/runtime-recovery.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print('Image confirmation + pending dialogue restart recovery passed', flush=True)


if __name__ == '__main__':
    main()
