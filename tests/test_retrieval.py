import pytest
from harness.resources.materials import read_material
from harness.core.open_engine import OpenEngine
from harness.resources.toolbox import PAGE_BREAK, chunks, retrieve
from test_open_tasks import FakeOpenModel


def pdf(pages):
    """Minimal text PDF with one Helvetica line per page."""
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', None, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'
        objects.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R '
                       '/Resources << /Font << /F1 3 0 R >> >> >>')
        kids.append(f'{len(objects)} 0 R')
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'
    out, offsets = '%PDF-1.4\n', []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n{body}\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n' + ''.join(f'{o:010d} 00000 n \n' for o in offsets)
    out += f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'
    return out.encode('latin-1')


def test_pdf_pages_become_paged_chunks(tmp_path):
    path = tmp_path / 'notes.pdf'
    path.write_bytes(pdf(['Photosynthesis makes glucose in chloroplasts.', 'Newton second law relates force and acceleration.']))
    material = read_material(path)
    assert material.count(PAGE_BREAK) == 1
    sources = chunks(material)
    assert [x['page'] for x in sources] == [1, 2]
    assert all(material[x['start']:x['end']] == x['text'] for x in sources)
    assert retrieve(sources, 'What does Newton second law say about force?')[0]['page'] == 2


def test_text_material_has_no_pages_and_page_breaks_are_stripped(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('第一段' + PAGE_BREAK + '第二段', encoding='utf-8')
    material = read_material(path)
    assert PAGE_BREAK not in material
    assert 'page' not in chunks(material)[0]


def test_scanned_pdf_is_rejected(tmp_path):
    path = tmp_path / 'scan.pdf'
    path.write_bytes(pdf(['']))
    with pytest.raises(ValueError, match='扫描件'):
        read_material(path)


def test_bm25_prefers_rare_matching_terms():
    common = '力是物体之间的相互作用。' * 3
    sources = chunks(common + '\n' * 300 + common + '惯性是物体保持原有运动状态的性质。')
    assert len(sources) >= 2
    assert '惯性' in retrieve(sources, '什么是惯性和力', limit=1)[0]['text']
    assert retrieve(chunks('只有一段关于惯性的材料。'), '惯性')
    assert retrieve(chunks('Only one paragraph.'), 'inertia') == []


def test_task_records_cited_pages(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('A', '解释光合作用', material='光合作用在叶绿体中进行。' + PAGE_BREAK + '牛顿第二定律。', mode='learn')
    assert s['task']['source_pages'] == [1]
