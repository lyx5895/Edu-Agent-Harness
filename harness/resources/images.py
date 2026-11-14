"""Validate uploaded images and bound their size before they reach the model."""

import base64
import hashlib
import io
import os
import re
from pathlib import Path

from PIL import Image, ImageOps

# Qwen3.5 vision encodes roughly one token per 32x32 pixels; 1536px keeps an
# image under ~2.3k tokens while handwriting stays legible.
MAX_SIDE = int(os.getenv('OMNIEDU_IMAGE_MAX_SIDE', '1536'))


def normalize_image(url, max_side=MAX_SIDE):
    if not isinstance(url, str) or len(url) > 12_000_000 or not re.match(r'^data:image/(png|jpeg|webp);base64,', url):
        raise ValueError('图片须为PNG/JPEG/WebP，最大8MB')
    try:
        raw = base64.b64decode(url.split(',', 1)[1], validate=True)
        im = Image.open(io.BytesIO(raw))
        if im.width * im.height > 20_000_000:
            raise ValueError('图片过大')
        im.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        # UnidentifiedImageError is an OSError; binascii.Error is a ValueError.
        raise ValueError('图片无法读取') from e
    if max(im.size) <= max_side and im.getexif().get(0x0112, 1) == 1:
        return url
    rotated = ImageOps.exif_transpose(im)
    rotated.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    if im.format == 'PNG':
        rotated.save(out, format='PNG', optimize=True)
        kind = 'png'
    else:
        rotated.convert('RGB').save(out, format='JPEG', quality=90)
        kind = 'jpeg'
    return f'data:image/{kind};base64,' + base64.b64encode(out.getvalue()).decode()


def store_image(url, root):
    """Write a normalized data URL to a content-addressed file; return the event fields."""
    kind, data = re.match(r'^data:image/(png|jpeg|webp);base64,(.*)$', url, re.S).groups()
    raw = base64.b64decode(data)
    digest = hashlib.sha256(raw).hexdigest()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'{digest}.{kind}'
    if not path.exists():
        tmp = path.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_bytes(raw)
        tmp.replace(path)
    return {'image_file': path.name, 'image_sha256': digest, 'image_bytes': len(raw)}


def load_image(payload, root):
    """Data URL for an image event; older events stored the data URL inline."""
    if payload.get('image_url'):
        return payload['image_url']
    name = payload['image_file']
    raw = (Path(root) / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != payload['image_sha256']:
        raise ValueError('原始图片文件校验失败')
    return f'data:image/{name.rsplit(".", 1)[1]};base64,' + base64.b64encode(raw).decode()


def file_to_data_url(path, max_side=MAX_SIDE):
    with Image.open(path) as im:
        fmt = (im.format or '').lower()
        if fmt not in ('png', 'jpeg', 'webp'):
            raise ValueError('图片须为PNG/JPEG/WebP')
        if im.width * im.height > 20_000_000:
            raise ValueError('图片过大')
        rotated = ImageOps.exif_transpose(im)
        rotated.thumbnail((max_side, max_side), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == 'png':
            rotated.save(out, format='PNG', optimize=True)
        else:
            fmt = 'jpeg'
            rotated.convert('RGB').save(out, format='JPEG', quality=90)
    return f'data:image/{fmt};base64,' + base64.b64encode(out.getvalue()).decode()
