"""Resumable fixed-revision download; verify model SHA256 before serving."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = 'lhpku20010120/Omni-Edu-9B'
REV = '7fda599d16f5c7aff770e2869450684c8e4d72f0'
SHA = '2740672b4b820e045c7af782cde9935836631fef5c3c7649751d375cf5560bcc'
FILES = [
    'config.json',
    'generation_config.json',
    'processor_config.json',
    'tokenizer.json',
    'tokenizer_config.json',
    'chat_template.jinja',
    'LICENSE',
    'NOTICE',
    'README.md',
    'model.safetensors',
]


def main():
    dest = ROOT / 'models/Omni-Edu-9B'
    dest.mkdir(parents=True, exist_ok=True)
    endpoints = list(
        dict.fromkeys([os.getenv('HF_ENDPOINT', 'https://hf-mirror.com').rstrip('/'), 'https://huggingface.co'])
    )
    for name in FILES:
        target = dest / name
        if name == 'model.safetensors' and target.exists() and target.stat().st_size == 18819722392:
            continue
        for endpoint in endpoints:
            resume = ['-C', '-'] if name == 'model.safetensors' else []
            r = subprocess.run(
                ['curl', '-fL', '--retry', '3', *resume, f'{endpoint}/{REPO}/resolve/{REV}/{name}', '-o', str(target)]
            )
            if r.returncode == 0:
                break
        else:
            raise RuntimeError(f'Download failed: {name}')
    verify(dest)


def verify(dest):
    p = dest / 'model.safetensors'
    h = hashlib.sha256()
    with p.open('rb') as f:
        while b := f.read(16 * 1024**2):
            h.update(b)
    assert h.hexdigest() == SHA, 'Weight checksum mismatch'
    assert json.loads((dest / 'config.json').read_text())['dtype'] == 'bfloat16'
    (dest / 'revision.txt').write_text(REV + '\n')
    (ROOT / 'reports/model-verification.json').write_text(
        json.dumps(
            {'repo': REPO, 'revision': REV, 'bytes': p.stat().st_size, 'sha256': h.hexdigest(), 'bf16': True}, indent=2
        )
    )
    print('Verified:', p)


if __name__ == '__main__':
    import sys

    verify(ROOT / 'models/Omni-Edu-9B') if '--verify-only' in sys.argv else main()
