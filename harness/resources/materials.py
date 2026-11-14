"""Turn uploaded teaching materials into plain text; PDF pages are kept as form-feed separated pages."""
from pathlib import Path
from harness.resources.toolbox import PAGE_BREAK

MAX_BYTES = 20 * 1024**2


def read_material(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES: raise ValueError('材料文件需小于20MB')
    if path.suffix.lower() != '.pdf':
        return path.read_text(encoding='utf-8').replace(PAGE_BREAK, '\n')
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        pages = [(page.extract_text() or '').replace(PAGE_BREAK, '\n') for page in PdfReader(path).pages]
    except PdfReadError as e:
        raise ValueError(f'无法读取PDF：{e}') from None
    if not any(p.strip() for p in pages): raise ValueError('PDF中没有可提取的文字（可能是扫描件），请上传文字版或改用图片题目。')
    return PAGE_BREAK.join(pages)
