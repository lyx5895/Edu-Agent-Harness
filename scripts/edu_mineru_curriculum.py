"""Parse scanned curriculum-standard PDFs (义务教育课程标准 2022年版) with the MinerU 精准解析 API.

The 2022 compulsory-education PDFs from MOE are image-only scans. MinerU (mineru.net) OCRs them and does the layout
work (reading order, headers/footers, paragraphs, tables); its ``*_content_list.json`` keeps the page of every block,
which the build needs for citations.

    MINERU_API_TOKEN=... python scripts/edu_mineru_curriculum.py OUT_DIR math=数学.pdf chinese=语文.pdf ...
    MINERU_API_TOKEN=... python scripts/edu_mineru_curriculum.py --batch BATCH_ID OUT_DIR math=数学.pdf ...  # 接着取结果

For each ``id=pdf`` it uploads the file (split into ``page_ranges`` chunks when the PDF exceeds the 200-page limit),
polls until done and unpacks each result zip into ``OUT_DIR/<id>/<chunk>/``. A chunk already unpacked is skipped,
so an interrupted run can be repeated. ``OUT_DIR/<id>/source.json`` records the PDF hash and the chunks.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import httpx
from pypdf import PdfReader

API = 'https://mineru.net/api/v4'
MAX_PAGES = 200
CHUNK = 100  # pages per chunk when a PDF has to be split


def chunks(n_pages: int) -> list[tuple[int, int]]:
    if n_pages <= MAX_PAGES:
        return [(1, n_pages)]
    return [(a, min(a + CHUNK - 1, n_pages)) for a in range(1, n_pages + 1, CHUNK)]


def check(r: httpx.Response) -> dict:
    r.raise_for_status()
    body = r.json()
    if body.get('code') != 0:
        raise RuntimeError(f'MinerU 返回错误：{body}')
    return body['data']


def download(url: str) -> bytes:
    """The result CDN is reached directly first: through some HTTP proxies its TLS handshake is cut off."""
    err = None
    for attempt in range(6):
        for trust_env in (False, True):
            try:
                r = httpx.get(url, timeout=600, trust_env=trust_env)
                r.raise_for_status()
                return r.content
            except httpx.HTTPError as e:  # retried; the last failure is raised below
                err = e
        time.sleep(5 * (attempt + 1))
    raise err


def main():
    args = sys.argv[1:]
    batch = None
    if args[:1] == ['--batch']:
        batch, args = args[1], args[2:]
    if len(args) < 2:
        raise SystemExit(__doc__)
    token = os.environ.get('MINERU_API_TOKEN')
    if not token:
        raise SystemExit('需要 MINERU_API_TOKEN')
    out = Path(args[0])
    client = httpx.Client(timeout=300, headers={'Authorization': f'Bearer {token}'})

    files, jobs = [], {}  # jobs: data_id -> (subject, chunk dir)
    for arg in args[1:]:
        subject, _, pdf = arg.partition('=')
        pdf = Path(pdf)
        n = len(PdfReader(str(pdf)).pages)
        sdir = out / subject
        sdir.mkdir(parents=True, exist_ok=True)
        parts = chunks(n)
        (sdir / 'source.json').write_text(
            json.dumps(
                {
                    'source_file': pdf.name,
                    'source_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    'pages_total': n,
                    'chunks': [f'{a}-{b}' for a, b in parts],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding='utf-8',
        )
        for a, b in parts:
            cdir = sdir / f'{a}-{b}'
            if (cdir / 'done').exists():
                print(f'{subject} {a}-{b}: 已有结果，跳过', flush=True)
                continue
            data_id = f'{subject}-{a}-{b}'
            files.append({'name': pdf.name, 'data_id': data_id, 'is_ocr': True, 'page_ranges': f'{a}-{b}', 'path': pdf})
            jobs[data_id] = (subject, cdir)
    if not files:
        return

    if batch:
        print(f'接着轮询 batch {batch}', flush=True)
    else:
        data = check(
            client.post(
                f'{API}/file-urls/batch',
                json={
                    'files': [{k: v for k, v in f.items() if k != 'path'} for f in files],
                    'model_version': 'vlm',
                    'language': 'ch',
                    'enable_formula': True,
                    'enable_table': True,
                },
            )
        )
        batch = data['batch_id']
        print(f'batch {batch}: {len(files)} 个文件（中断后可用 --batch {batch} 接着取结果）', flush=True)
        # every chunk has its own upload slot, even when two chunks come from the same PDF
        for f, url in zip(files, data['file_urls']):
            r = httpx.put(url, content=f['path'].read_bytes(), timeout=600)  # presigned URL: no auth header
            r.raise_for_status()
            print(f'已上传 {f["data_id"]}', flush=True)

    pending = set(jobs)
    while pending:
        time.sleep(15)
        result = check(client.get(f'{API}/extract-results/batch/{batch}'))
        for item in result.get('extract_result', []):
            did = item.get('data_id')
            if did not in pending:
                continue
            state = item.get('state')
            if state == 'done':
                subject, cdir = jobs[did]
                cdir.mkdir(parents=True, exist_ok=True)
                zipfile.ZipFile(io.BytesIO(download(item['full_zip_url']))).extractall(cdir)
                (cdir / 'done').write_text(item['full_zip_url'], encoding='utf-8')
                pending.discard(did)
                print(f'{did}: 完成', flush=True)
            elif state == 'failed':
                pending.discard(did)
                print(f'{did}: 失败 {item.get("err_msg")}', file=sys.stderr, flush=True)
            else:
                prog = item.get('extract_progress') or {}
                print(f'{did}: {state} {prog.get("extracted_pages", "")}/{prog.get("total_pages", "")}', flush=True)


if __name__ == '__main__':
    main()
