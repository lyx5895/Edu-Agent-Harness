"""Task-image confirmation, material learning and restart recovery with real model."""

import base64
import io
import json
import subprocess
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {}
    image = Image.new('RGB', (700, 130), 'white')
    ImageDraw.Draw(image).text((20, 35), 'Explain why evaporation can occur below the boiling point.', fill='black')
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    with httpx.Client(base_url='http://127.0.0.1:7860', timeout=600, trust_env=False) as c:

        def post(path, data):
            r = c.post(path, json=data)
            if r.status_code != 200:
                raise RuntimeError(r.text)
            return r.json()

        s = post('/api/sessions', {'learner': 'image-recovery-' + uuid.uuid4().hex[:8], 'image_url': url})
        sid = s['session_id']
        path = '/api/sessions/' + sid
        assert s['task'] is None and s['pending_image']['text']
        confirmed = post(
            path + '/confirm_task', {'request_id': 'confirm', 'text': '为什么蒸发可以在低于沸点的温度发生？'}
        )
        assert confirmed['session']['task']['question'] == '为什么蒸发可以在低于沸点的温度发生？'
        post(path + '/hint', {'request_id': 'hint'})
        before = c.get(path).json()
        assert before['pending_question']
        for action in ('stop', 'start'):
            subprocess.run(
                [str(ROOT / '.envs/harness/bin/python'), str(ROOT / 'scripts/service.py'), action, 'harness'],
                cwd=ROOT,
                check=True,
            )
        after = c.get(path).json()
        assert after == before
        reply = post(path + '/reply', {'request_id': 'reply', 'text': '这个说明没有帮助，我还是不懂蒸发和沸腾的区别。'})
        assert reply['action'] == 'change_explanation' and reply['evidence'] == 'dialogue_only'
        learning = post(
            '/api/sessions',
            {
                'learner': 'material-learning-' + uuid.uuid4().hex[:8],
                'mode': 'learn',
                'task': '学习信息编码',
                'material': '二进制只有零和一两种数字。八个二进制位构成一个字节。编码通过约定把符号或信息转换为特定形式。',
            },
        )
        assert learning['task']['source_ids'] and learning['task']['question'] != '学习信息编码'
        feedback = post('/api/sessions/' + learning['session_id'] + '/explain', {'request_id': 'explain'})
        assert feedback['action'] == 'explain'
        report = {
            'passed': True,
            'image_session': sid,
            'transcription': s['pending_image'],
            'identical_after_restart': before == after,
            'continued_reply': reply,
            'material_learning_task': learning['task'],
            'material_explanation': feedback['feedback'],
        }
    (ROOT / 'reports/open-task-recovery.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Open task image + material + recovery passed', flush=True)


if __name__ == '__main__':
    main()
