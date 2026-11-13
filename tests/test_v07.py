import base64
import io
import json
import time

import httpx
import pytest
from PIL import Image

from harness.images import file_to_data_url, normalize_image
from harness.model import InputTooLong, ModelClient
from harness.open_engine import OpenEngine
from harness.tasks import Assessment, choose
from test_open_tasks import FakeOpenModel, act
from test_teaching_v04 import assigned


def data_url(im, fmt='PNG', **save):
    buf = io.BytesIO()
    im.save(buf, format=fmt, **save)
    return f'data:image/{fmt.lower()};base64,' + base64.b64encode(buf.getvalue()).decode()


def decode(url):
    return Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1])))


def test_large_photo_is_downscaled_and_small_image_untouched():
    small = data_url(Image.new('RGB', (64, 64), 'white'))
    assert normalize_image(small) == small
    big = normalize_image(data_url(Image.new('RGB', (3024, 4032), 'white'), 'JPEG'))
    assert big.startswith('data:image/jpeg') and max(decode(big).size) == 1536
    with pytest.raises(ValueError):
        normalize_image('data:image/png;base64,AA==')


def test_exif_rotation_is_applied():
    exif = Image.Exif()
    exif[0x0112] = 6
    url = normalize_image(data_url(Image.new('RGB', (200, 100), 'white'), 'JPEG', exif=exif))
    assert decode(url).size == (100, 200)


def test_file_upload_is_downscaled(tmp_path):
    path = tmp_path / 'photo.jpg'
    Image.new('RGB', (4000, 3000), 'white').save(path, format='JPEG')
    assert max(decode(file_to_data_url(path)).size) == 1536


def fake_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def test_structured_call_sends_json_schema(monkeypatch, tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        content = json.dumps({'status': 'incorrect', 'confidence': 'high', 'evidence_quote': '2x+3',
                              'observation': '漏乘', 'hypothesis': '分配律'})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}], 'usage': {}})

    fake_transport(monkeypatch, handler)
    m = ModelClient(base_url='http://model/v1')
    m.logs = tmp_path / 'calls.jsonl'
    result = m.structured(Assessment, '判定', '2x+3')
    assert result.status == 'incorrect'
    fmt = seen[0]['response_format']
    assert fmt['type'] == 'json_schema' and fmt['json_schema']['schema']['properties']['status']['enum']
    assert '判定' in seen[0]['messages'][0]['content']


def test_context_overflow_has_specific_error(monkeypatch):
    fake_transport(monkeypatch, lambda request: httpx.Response(400, json={'error': {
        'message': "Input length (11860) exceeds model's maximum context length (4096)."}}))
    with pytest.raises(InputTooLong, match='过长'):
        ModelClient(base_url='http://model/v1').chat([{'role': 'user', 'content': 'x'}])


def test_hints_escalate_through_three_levels(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('u', '解释四季成因')
    actions = [act(e, s, 'hint')['action'] for _ in range(4)]
    assert actions == ['scaffold_concept', 'scaffold_step', 'scaffold_example', 'scaffold_example']
    assert e.replay(s['session_id'])['passed']
    assert choose({'phase': 'teach', 'kind': 'hint', 'fallbacks': 2, 'status': 'unknown'})['action'] == 'teacher_review'


class SlowCriteriaModel(FakeOpenModel):
    def assess_criterion(self, task, criterion, text, context):
        time.sleep(0.3)
        return dict(status='correct', confidence='high', evidence_quote=text, observation='符合', hypothesis='')

    def assess(self, task, text, context, verification):
        time.sleep(0.3)
        return super().assess(task, text, context, verification)


def test_criteria_and_whole_task_are_graded_concurrently(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', SlowCriteriaModel())
    criteria = [dict(id=f'c{i}', kind='model', description=f'要求{i}') for i in range(3)]
    s = assigned(e, criteria=criteria)
    started = time.monotonic()
    r = act(e, s, 'attempt', '参考结论')
    assert time.monotonic() - started < 0.9
    assert r['status'] == 'correct'
    assert [x['id'] for x in r['criteria_results']] == ['c0', 'c1', 'c2']


def test_review_interval_expands_with_independent_streak(tmp_path):
    from harness.memory import summarize
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    for _ in range(2):
        s = assigned(e)
        act(e, s, 'attempt', '参考结论'); act(e, s, 'check'); act(e, s, 'attempt', '参考结论')
    with e.connect() as c:
        memory = summarize(c, '学生A')[0]
        assert memory['independent_streak'] == 2 and memory['review_interval_days'] == 3
        assert not summarize(c, '学生A', now=time.time() + 2*86400)[0]['review_due']
        assert summarize(c, '学生A', now=time.time() + 3*86400 + 60)[0]['review_due']


def test_review_queue_flags_risky_model_judgments(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('u', '解释四季成因')
    act(e, s, 'attempt', '参考结论')
    item = e.dashboard()['review_queue'][0]
    assert item['priority'] == 'risk' and 'model_reference' in item['risks']
