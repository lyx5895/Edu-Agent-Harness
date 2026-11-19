"""Minimal .docx and printable HTML writers for teacher deliverables (COURSE_WORKSPACE_DESIGN.md 9.1).

No third-party dependency: a .docx is a zip of a few XML parts. Documents are built from a neutral block list:
``('h1'|'h2'|'p'|'meta'|'li', text)``, ``('table', rows)`` with the first row as header, ``('blank', n)`` for
answer space. Formulas stay as plain text.
"""

from __future__ import annotations

import io
import zipfile
from html import escape
from xml.sax.saxutils import escape as xml_escape

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)
SIZES = {'h1': 36, 'h2': 28, 'p': 22, 'li': 22, 'meta': 18}


def _run(text, *, size=22, bold=False, color=None):
    props = f'<w:rFonts w:ascii="Arial" w:eastAsia="SimSun"/><w:sz w:val="{size}"/>'
    if bold:
        props = '<w:b/>' + props
    if color:
        props += f'<w:color w:val="{color}"/>'
    return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'


def _para(text, kind='p'):
    lines = str(text).split('\n')
    runs = '<w:r><w:br/></w:r>'.join(
        _run(x, size=SIZES[kind], bold=kind in ('h1', 'h2'), color='666666' if kind == 'meta' else None) for x in lines
    )
    prefix = _run('• ', size=SIZES[kind]) if kind == 'li' else ''
    return f'<w:p><w:pPr><w:spacing w:after="120"/></w:pPr>{prefix}{runs}</w:p>'


def _table(rows):
    border = ''.join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
        for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV')
    )
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>']
    for i, r in enumerate(rows):
        cells = ''.join(f'<w:tc><w:p>{_run(str(cell), size=20, bold=i == 0)}</w:p></w:tc>' for cell in r)
        out.append(f'<w:tr>{cells}</w:tr>')
    out.append('</w:tbl>')
    return ''.join(out) + '<w:p/>'


def to_docx(blocks) -> bytes:
    body = []
    for kind, value in blocks:
        if kind == 'table':
            body.append(_table(value))
        elif kind == 'blank':
            body.extend('<w:p/>' for _ in range(int(value)))
        else:
            body.append(_para(value, kind))
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>'
        + ''.join(body)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1200" w:right="1100" w:bottom="1200" w:left="1100"/></w:sectPr></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CONTENT_TYPES)
        z.writestr('_rels/.rels', RELS)
        z.writestr('word/document.xml', document)
    return buf.getvalue()


def to_html(blocks, title) -> str:
    parts = []
    for kind, value in blocks:
        if kind == 'h1':
            parts.append(f'<h1>{escape(value)}</h1>')
        elif kind == 'h2':
            parts.append(f'<h2>{escape(value)}</h2>')
        elif kind == 'meta':
            parts.append(f'<p class="meta">{escape(value)}</p>')
        elif kind == 'li':
            parts.append(f'<ul><li>{escape(value)}</li></ul>')
        elif kind == 'blank':
            parts.append('<div class="blank"></div>' * int(value))
        elif kind == 'table':
            head, *rows = value
            parts.append(
                '<table><tr>'
                + ''.join(f'<th>{escape(str(h))}</th>' for h in head)
                + '</tr>'
                + ''.join('<tr>' + ''.join(f'<td>{escape(str(c))}</td>' for c in r) + '</tr>' for r in rows)
                + '</table>'
            )
        else:
            parts.append(f'<p>{escape(str(value)).replace(chr(10), "<br>")}</p>')
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title><style>'
        'body{font-family:"Songti SC","SimSun",serif;max-width:780px;margin:24px auto;padding:0 16px;line-height:1.7;color:#111;background:#fff}'
        'h1{font-size:22px;text-align:center}h2{font-size:17px;margin-top:22px}.meta{color:#666;font-size:12px}'
        'table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border:1px solid #999;padding:4px 6px;font-size:14px;vertical-align:top}'
        'ul{margin:0;padding-left:22px}.blank{height:28px;border-bottom:1px dashed #ccc}'
        '.bar{position:sticky;top:0;background:#fff;padding:8px 0;text-align:right}'
        '@media print{.bar{display:none}body{margin:0;max-width:none}}'
        '</style></head><body><div class="bar"><button onclick="window.print()">打印 / 另存为 PDF</button></div>'
        + ''.join(parts)
        + '</body></html>'
    )
