"""Range downloader with resumable verified-length parts and final SHA256."""
import concurrent.futures
import os
import time
import httpx
from download_model import ROOT, REPO, REV, verify

SIZE = 18819722392
CHUNK = 64 * 1024**2
dest = ROOT / 'models/Omni-Edu-9B'
parts = dest / '.parts'
parts.mkdir(exist_ok=True)

def download(i):
    start, end = i * CHUNK, min(SIZE, (i + 1) * CHUNK) - 1
    out = parts / str(i)
    if out.exists() and out.stat().st_size == end - start + 1:
        return i
    for trial in range(6):
        endpoint = os.environ.get('HF_ENDPOINT', 'https://hf-mirror.com') if trial == 0 else 'https://huggingface.co'
        try:
            with httpx.Client(follow_redirects=True, timeout=120) as client:
                # Unique query avoids intermediary caches incorrectly reusing a different range.
                with client.stream('GET', f'{endpoint}/{REPO}/resolve/{REV}/model.safetensors?part={i}',
                        headers={'Range': f'bytes={start}-{end}'}) as r:
                    r.raise_for_status()
                    assert r.status_code == 206 and r.headers.get('content-range', '').startswith(f'bytes {start}-{end}/')
                    with out.with_suffix('.tmp').open('wb') as f:
                        for b in r.iter_bytes(1024**2):
                            f.write(b)
            assert out.with_suffix('.tmp').stat().st_size == end - start + 1
            out.with_suffix('.tmp').replace(out)
            return i
        except Exception as e:
            print(f'part {i} retry {trial}: {type(e).__name__}', flush=True)
            time.sleep(min(2**trial, 20))
    raise RuntimeError(f'Failed part {i}')

if __name__ == '__main__':
    count = (SIZE + CHUNK - 1) // CHUNK
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for done, _ in enumerate(pool.map(download, range(count)), 1):
            print(f'{done}/{count} parts complete', flush=True)
    target = dest / 'model.safetensors'
    with target.open('wb') as f:
        for i in range(count):
            with (parts / str(i)).open('rb') as part:
                while b := part.read(8 * 1024**2):
                    f.write(b)
    verify(dest)
