"""Photos of student work: validate, turn upright, bound the size and store them by content hash."""

import hashlib
import io
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps

# Whole exam pages: 2048px on the long side keeps handwriting legible at a bounded token cost.
MAX_SIDE = 2048
MAX_BYTES = 15 * 1024**2
MAX_PIXELS = 60_000_000
NAME = re.compile(r'^[0-9a-f]{64}\.jpeg$')


def prepare(data: bytes) -> tuple[str, bytes]:
    """Validate a photo and return (file name, normalized JPEG bytes)."""
    if not data:
        raise ValueError('照片是空文件')
    if len(data) > MAX_BYTES:
        raise ValueError('每张照片需小于 15MB')
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or '').upper()
        if im.width * im.height > MAX_PIXELS:
            raise ValueError('照片像素过大')
        im.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise ValueError('照片无法读取（支持 JPG、PNG、WebP；苹果手机的 HEIC 请先转成 JPG）') from e
    if fmt not in ('JPEG', 'MPO', 'PNG', 'WEBP'):
        raise ValueError('照片需为 JPG、PNG 或 WebP 格式')
    im = ImageOps.exif_transpose(im)  # phones store the rotation in EXIF
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    im.convert('RGB').save(out, format='JPEG', quality=88)
    raw = out.getvalue()
    return hashlib.sha256(raw).hexdigest() + '.jpeg', raw


def path(name: str, root) -> Path:
    """Path of a stored photo; only content-hash names are accepted (no traversal)."""
    if not NAME.match(name or ''):
        raise ValueError('照片名称无效')
    return Path(root) / name


def store(name: str, raw: bytes, root) -> Path:
    target = path(name, root)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        tmp.write_bytes(raw)
        tmp.replace(target)
    return target
