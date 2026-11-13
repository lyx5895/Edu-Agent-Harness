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


def test_verbatim_quote_salvages_garbled_tail_but_rejects_paraphrase():
    from harness.open_engine import verbatim_quote
    text = 'She also spent 25 tickets on the koala, so she had 50 tickets in total.'
    assert verbatim_quote('spent 25 tickets', text) == 'spent 25 tickets'
    garbled = 'She also spent 25 tickets on the koala, so she had 50 ticketz'
    assert verbatim_quote(garbled, text) == 'She also spent 25 tickets on the koala, so she had 50 ticket'
    assert verbatim_quote('She also spent 25 tickets...', text) == 'She also spent 25 tickets'
    assert verbatim_quote('Her total was fifty tickets', text) is None
    assert verbatim_quote('', text) is None


def test_images_are_files_and_traces_store_diffs(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    url = data_url(Image.new('RGB', (32, 32), 'white'))
    s = e.create('u', image_url=url)
    with e.connect() as c:
        event = c.execute("SELECT id,payload FROM learning_events WHERE kind='image'").fetchone()
    payload = json.loads(event['payload'])
    assert 'image_url' not in payload and (e.images / payload['image_file']).exists()
    assert e.image_event(s['session_id'], event['id'])['image_url'] == url
    act(e, s, 'confirm_task', 'Explain tides.')
    trace = e.traces(s['session_id'])[-1]
    assert 'state_before' not in trace and trace['state_diff']['task']['before'] is None


def test_paraphrase_is_bound_to_chosen_sentence():
    from harness.open_engine import ground
    text = 'Berry read 262 pages. Therefore, Berry has to read 88 pages on Saturday.'
    paraphrase = {'evidence_quote': 'He has to read 88 pages on Saturday', 'evidence_sentence': 2}
    assert ground(paraphrase, text) == 'Therefore, Berry has to read 88 pages on Saturday.'
    assert ground({**paraphrase, 'evidence_sentence': 9}, text) is None
    assert ground({'evidence_quote': 'read 262 pages', 'evidence_sentence': 2}, text) == 'read 262 pages'


class ParaphrasingModel(FakeOpenModel):
    def __init__(self):
        super().__init__()
        self.assess_contexts = []

    def assess(self, task, text, context, verification):
        self.assess_contexts.append(context)
        result = super().assess(task, text, context, verification)
        if 'quote_feedback' not in context:
            result['evidence_quote'] = '学生大意是说了一个结论'
        return result


def test_paraphrased_quote_is_retried_once_with_feedback(tmp_path):
    model = ParaphrasingModel()
    e = OpenEngine(tmp_path / 'db.sqlite', model)
    s = e.create('u', '解释四季成因')
    r = act(e, s, 'attempt', '参考结论')
    assert r['status'] == 'correct' and len(model.assess_contexts) == 2
    assert 'rejected_assessment' in e.traces(s['session_id'])[-1]


def test_ungrounded_judgment_becomes_unknown_for_teacher(tmp_path):
    model = FakeOpenModel()
    original = model.assess
    model.assess = lambda *a: dict(original(*a), evidence_quote='并非学生原文的句子')
    e = OpenEngine(tmp_path / 'db.sqlite', model)
    s = e.create('u', '解释四季成因')
    r = act(e, s, 'attempt', '参考结论')
    assert r['status'] == 'unknown' and not r['session']['completed']
    assert e.dashboard()['review_queue'][0]['priority'] == 'urgent'


def test_fallback_follows_task_language(tmp_path):
    model = FakeOpenModel()
    model.bad_review = True
    e = OpenEngine(tmp_path / 'db.sqlite', model)
    english = act(e, e.create('u', 'Explain why the moon has phases.'), 'attempt', 'wrong idea')
    chinese = act(e, e.create('u', '解释月相成因'), 'attempt', '错误解释')
    assert english['feedback'].isascii() and not chinese['feedback'].isascii()
    model.bad_review = False
    translated = act(e, e.create('u3', 'Explain why the sky is blue.'), 'attempt', 'wrong idea')
    assert translated['feedback'] == 'Translated: what is your reasoning?'


def test_policy_v4_locates_grounded_error_and_replays(tmp_path):
    model = FakeOpenModel()
    model.candidate = 'The step "2+2=5" adds incorrectly; what does 2+2 give?'
    e = OpenEngine(tmp_path / 'db.sqlite', model, policy='open-task-policy-v4')
    s = e.create('u', 'Explain why the moon has phases.')
    out = act(e, s, 'attempt', '2+2=5')
    assert out['action'] == 'locate' and out['feedback'] == model.candidate
    assert e.replay(s['session_id'])['passed']
    model.bad_review = True
    again = act(e, e.create('u2', 'Explain why tides happen.'), 'attempt', 'tides 2+3=6')
    assert again['feedback'] == 'Take another look at this part: "tides 2+3=6". What is this step based on, and does it hold?'
    trace = [t for t in e.traces(again['session']['session_id']) if 'output_validation' in t][-1]
    assert trace['output_validation']['delivered_action'] == 'locate_template'
    v3 = OpenEngine(tmp_path / 'v3.sqlite', FakeOpenModel(), policy='open-task-policy-v3')
    assert act(v3, v3.create('u', 'Explain tides.'), 'attempt', 'wrong')['action'] == 'probe'
    with pytest.raises(ValueError):
        OpenEngine(tmp_path / 'x.sqlite', model, policy='nope')


def test_review_queue_flags_risky_model_judgments(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('u', '解释四季成因')
    act(e, s, 'attempt', '参考结论')
    item = e.dashboard()['review_queue'][0]
    assert item['priority'] == 'risk' and 'model_reference' in item['risks']


def test_policy_v5_escalates_hints_when_probing_stalls(tmp_path):
    from harness.tasks import choose_v5
    base = {'phase': 'teach', 'kind': 'reply', 'status': 'unknown', 'failures': 1, 'hints': 0}
    assert choose_v5({**base, 'outcome': 'not_helped', 'stalls': 1})['action'] == 'change_explanation'
    assert choose_v5({**base, 'outcome': 'not_helped', 'stalls': 2})['action'] == 'scaffold_concept'
    assert choose_v5({**base, 'outcome': 'helped', 'stalls': 2})['action'] == 'self_correct'
    assert choose_v5({**base, 'outcome': 'helped', 'stalls': 3})['action'] == 'scaffold_concept'
    assert choose_v5({**base, 'outcome': 'misconception', 'stalls': 2})['action'] == 'prerequisite'
    assert choose_v5({**base, 'outcome': 'misconception', 'stalls': 3})['action'] == 'scaffold_concept'
    assert choose_v5({**base, 'outcome': 'not_helped', 'stalls': 2, 'hints': 3})['action'] == 'teacher_review'
    model = FakeOpenModel()
    model.outcome = 'not_helped'
    e = OpenEngine(tmp_path / 'db.sqlite', model, policy='open-task-policy-v5')
    s = e.create('u', '解释四季成因')
    actions = [act(e, s, 'attempt', '错误解释')['action']] + [act(e, s, 'reply', '我觉得没错')['action'] for _ in range(5)]
    assert actions == ['locate', 'scaffold_concept', 'change_explanation', 'scaffold_step', 'change_explanation', 'scaffold_example']
    assert e.replay(s['session_id'])['passed']


class FakeReviewer:
    model_name = 'reviewer-27b'
    def __init__(self, status='incorrect', acceptable=True):
        self.status, self.acceptable, self.calls = status, acceptable, []
    def assess(self, task, text, context, verification):
        self.calls.append('assess')
        return {'status': self.status, 'confidence': 'high', 'evidence_quote': text, 'observation': '复核意见', 'hypothesis': ''}
    def review(self, task, candidate, action, student=None):
        self.calls.append('review')
        return {'acceptable': self.acceptable, 'reason': '复核'}


def test_cross_review_confirms_negative_judgment_and_relieves_queue(tmp_path):
    reviewer = FakeReviewer('incorrect')
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel(), reviewer=reviewer)
    out = act(e, e.create('u', '解释四季成因'), 'attempt', '错误解释')
    assert out['status'] == 'incorrect' and reviewer.calls == ['assess', 'review']
    queue = e.dashboard()['review_queue']
    assert all('negative_judgment' not in item['risks'] for item in queue)


def test_cross_review_disagreement_goes_to_teacher(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel(), reviewer=FakeReviewer('correct'))
    out = act(e, e.create('u', '解释四季成因'), 'attempt', '错误解释')
    assert out['status'] == 'unknown' and out['action'] == 'clarify'
    assert e.dashboard()['review_queue'][0]['priority'] == 'urgent'


def test_cross_review_can_veto_feedback_without_replay_collision(tmp_path):
    reviewer = FakeReviewer('incorrect', acceptable=False)
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel(), policy='open-task-policy-v3', reviewer=reviewer)
    s = e.create('u', '解释四季成因')
    act(e, s, 'attempt', '错误解释')
    trace = [t for t in e.traces(s['session_id']) if 'output_validation' in t][-1]['output_validation']
    assert trace['reasons'] == ['cross_review'] and trace['cross_review']['reviewer'] == 'reviewer-27b'
    assert act(e, e.create('u2', '解释潮汐'), 'attempt', '正确解释')['status'] == 'correct'
    assert reviewer.calls.count('assess') == 1


def test_locate_trailing_instruction_is_trimmed_and_template_is_not_generic_fallback(tmp_path):
    model = FakeOpenModel()
    model.candidate = 'The step "2+2=5" looks off; what does 2+2 give? Please recheck it.'
    e = OpenEngine(tmp_path / 'db.sqlite', model)
    s = e.create('u', 'Explain why the moon has phases.')
    assert act(e, s, 'attempt', '2+2=5')['feedback'] == 'The step "2+2=5" looks off; what does 2+2 give?'
    model.bad_review = True
    s2 = e.create('u2', 'Explain why tides happen.')
    act(e, s2, 'attempt', 'tides 2+3=6')
    with e.connect() as c:
        assert e.load(c, s2['session_id'])[1]['feedback_fallbacks'] == 0
