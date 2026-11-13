"""Validate uploaded images and bound their size before they reach the model."""
import base64
import io
import os
import re

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
    except Exception as e:
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
