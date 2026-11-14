import json
import uuid
from typing import ClassVar

import pytest

from harness.core.gateway import Gateway
from harness.core.open_engine import OpenEngine
from harness.errors import Conflict
from harness.llm.model import ModelUnavailable
from harness.resources.toolbox import Toolbox


class FakeOpenModel:
    base_url = 'fake'
    calls: ClassVar[list] = []

    def __init__(self):
        self.outcome = 'misconception'
        self.bad_review = False
        self.fail = False
        self.candidate = '你能说明判断的依据吗？'
        self.plans = 0

    def plan(self, request, sources, mode, previous):
        self.plans += 1
        return {
            'domain': '自定义领域',
            'objective': '解释现象的原因',
            'question': f'新的材料问题{self.plans}' if previous or mode == 'learn' else request,
            'rubric': '解释须与材料一致',
            'reference': '参考结论',
            'source_quote': sources[0]['text'][:10] if sources else '',
            'prerequisite': '理解因果关系',
            'algebra_initial': '',
            'algebra_kind': 'none',
        }

    def assess(self, task, text, context, verification):
        if self.fail:
            raise ModelUnavailable('timeout')
        return {
            'status': 'correct' if text in ('正确解释', task['reference']) else 'incorrect',
            'confidence': 'high',
            'evidence_quote': text,
            'observation': '关键依据待检查',
            'hypothesis': '可能混淆因果',
        }

    def interpret_reply(self, packet):
        return {'outcome': self.outcome, 'evidence_quote': packet['student_reply'], 'hypothesis': '待验证'}

    def respond(self, *a):
        if self.fail:
            raise ModelUnavailable('timeout')
        return self.candidate

    def review(self, *a):
        return {'acceptable': not self.bad_review, 'reason': '检查结果'}

    def translate(self, text, target='English'):
        return 'Translated: what is your reasoning?'

    def transcribe(self, url):
        return {'text': 'Explain why the moon has phases.', 'uncertain': False, 'note': ''}


@pytest.fixture
def setup(tmp_path):
    m = FakeOpenModel()
    e = OpenEngine(tmp_path / 'open.sqlite', m)
    return e, m


def act(e, s, kind, text='', key=None):
    return e.operation(s['session_id'], key or uuid.uuid4().hex, kind, {'text': text})


@pytest.mark.parametrize(
    'task', ['解释四季成因', '修改这段英语作文', '分析杜甫诗歌的意象', '解释二叉树遍历', '讨论供给和需求']
)
def test_arbitrary_subject_no_course_required(setup, task):
    e, _m = setup
    s = e.create('u', task)
    assert s['task']['question'] == task and s['engine'] == 'open-task-v1'
    assert 'reference' not in json.dumps(s)
    assert 'k01' not in json.dumps(s)
    r = act(e, s, 'attempt', '错误解释')
    assert r['action'] == 'locate' and r['status'] == 'incorrect'
    r = act(e, s, 'reply', '我以为一直如此')
    assert r['action'] == 'prerequisite' and r['evidence'] == 'dialogue_only'
    r = act(e, s, 'attempt', '正确解释')
    assert r['evidence'] == 'assisted_complete'
    check = act(e, s, 'check')
    assert check['session']['task']['phase'] == 'check'
    with pytest.raises(Conflict):
        act(e, s, 'hint')
    final = act(e, s, 'attempt', '正确解释')
    assert final['evidence'] == 'independent_model_assessed'
    assert e.replay(s['session_id'])['passed']


def test_material_sources_and_learning_mode(setup):
    e, _m = setup
    s = e.create('u', '理解材料', '蒸发可以发生在任何温度。' * 100, 'learn')
    assert s['task']['question'].startswith('新的材料问题')
    assert s['task']['source_ids']
    act(e, s, 'attempt', '错误解释')
    trace = e.traces(s['session_id'])[0]
    assert len(trace['context']['materials']) <= 2
    assert all(x['id'].startswith('material:') for x in trace['context']['materials'])


def test_invented_material_quote_aborts_creation(setup):
    e, m = setup
    plan = m.plan

    def bad(*args):
        return dict(plan(*args), source_quote='不存在的原文')

    m.plan = bad
    with pytest.raises(ModelUnavailable):
        e.create('u', '任务', '原文')
    assert e.report() == []


def test_idempotency_create_turn_and_failure_rollback(setup):
    e, m = setup
    s = e.create('u', '任务', request_id='create')
    assert e.create('u', '任务', request_id='create') == s and m.plans == 1
    with pytest.raises(Conflict):
        e.create('u', '别的任务', request_id='create')
    m.fail = True
    with pytest.raises(ModelUnavailable):
        act(e, s, 'attempt', '错误解释', key='retry')
    assert e.session(s['session_id']) == s
    m.fail = False
    r = act(e, s, 'attempt', '错误解释', key='retry')
    assert act(e, s, 'attempt', '错误解释', key='retry') == r
    assert len(e.traces(s['session_id'])) == 1


def test_refused_new_check_leaves_original_task(setup):
    e, m = setup
    s = e.create('u', '任务')
    act(e, s, 'attempt', '正确解释')
    before = e.session(s['session_id'])
    m.bad_review = True
    with pytest.raises(ModelUnavailable):
        act(e, s, 'check')
    assert e.session(s['session_id']) == before


def test_review_blocks_leak_but_allows_subject_symbols(setup):
    e, m = setup
    s = e.create('u', '解释 why 与 because 的用法')
    m.candidate = 'because 引导的从句在这里表达什么关系？'
    r = act(e, s, 'hint')
    assert 'because' in r['feedback']
    m.candidate = '答案就是参考结论'
    r = act(e, s, 'hint')
    assert '参考结论' not in r['feedback']
    assert e.traces(s['session_id'])[-1]['output_validation']['fallback']
    m.candidate = '合理但审核不通过的输出'
    m.bad_review = True
    r = act(e, s, 'hint')
    assert r['feedback'] != m.candidate


def test_teacher_revision_and_restart(setup):
    e, m = setup
    s = e.create('u', '任务')
    r = act(e, s, 'attempt', '正确解释')
    e = OpenEngine(e.db, m)
    assert e.session(s['session_id'])['completed']
    e.correct(s['session_id'], r['event_id'], 'unknown', '材料存在歧义', 'fix')
    report = e.report('u')[0]['turns'][0]
    assert report['original_status'] == 'correct' and report['status'] == 'unknown'
    assert report['evidence'] == 'teacher_revised_needs_recheck'
    with pytest.raises(Conflict):
        act(e, s, 'check')


def test_image_task_needs_confirmation(setup):
    import base64
    import io

    from PIL import Image

    e, m = setup
    buf = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(buf, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    s = e.create('u', image_url=url)
    assert s['task'] is None and m.plans == 0
    with pytest.raises(Conflict):
        act(e, s, 'attempt', '作答')
    r = act(e, s, 'confirm_task', 'Explain tides instead.')
    assert r['session']['task']['question'] == 'Explain tides instead.'
    assert r['session']['pending_image'] is None


def test_algebra_is_optional_plugin_and_abstains():
    task = {'question': '解方程：3x+2=14', 'algebra_initial': '3x+2=14', 'algebra_kind': 'equation'}
    result, _ = Toolbox().verify(task, 'x=4')
    assert result['status'] == 'correct'
    assert Toolbox([]).verify(task, 'x=4')[0]['status'] == 'unknown'
    task['question'] = '解释潮汐成因'
    assert Toolbox().verify(task, 'x=4')[0]['status'] == 'unknown'


def test_open_api_default_requires_task_and_supports_nonmath(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import harness.app as app

    g = Gateway(db=tmp_path / 'api.sqlite', model=FakeOpenModel())
    monkeypatch.setattr(app, 'engine', g)
    with TestClient(app.app) as c:
        assert c.post('/api/sessions', json={'learner': 'u'}).status_code == 400
        s = c.post('/api/sessions', json={'learner': 'u', 'task': '解释季风形成'}).json()
        path = '/api/sessions/' + s['session_id']
        r = c.post(path + '/attempt', json={'request_id': 'a', 'text': '错误解释'}).json()
        assert r['action'] == 'locate'
        assert c.get('/api/teacher/sessions/' + s['session_id'] + '/replay').json()['passed']
        assert c.get('/api/progress/u').json()['open_tasks'][0]['task']['question'] == '解释季风形成'
        assert c.get('/').status_code == 200


def test_core_has_no_course_imports():
    import ast
    from pathlib import Path

    for name in ('core/tasks', 'resources/toolbox', 'core/open_engine', 'llm/open_model'):
        tree = ast.parse(Path('harness', name + '.py').read_text())
        assert not any(
            isinstance(n, ast.ImportFrom) and (n.module or '').startswith('harness.legacy') for n in ast.walk(tree)
        )


def test_material_only_creates_learning_task(setup):
    e, _m = setup
    s = e.create('u', material='关于细胞的学习材料')
    assert s['task']['question'].startswith('新的材料问题')


def test_image_after_completion_is_rejected(setup):
    e, _m = setup
    s = e.create('u', '任务')
    act(e, s, 'attempt', '正确解释')
    with pytest.raises(Conflict):
        e.operation(s['session_id'], 'image', 'image', {'image_url': 'bad'})


def test_matching_history_cross_session_and_correction_exclusion(setup):
    e, _m = setup
    s = e.create('u', '任务')
    r = act(e, s, 'attempt', '错误解释')
    other = e.create('other', '任务')
    act(e, other, 'attempt', '错误解释')
    s2 = e.create('u', '任务')
    act(e, s2, 'attempt', '错误解释')
    prior = e.traces(s2['session_id'])[0]['context']['prior_task_evidence']
    assert len(prior) == 1 and prior[0]['session_id'] == s['session_id']
    e.correct(s['session_id'], r['event_id'], 'unknown', '误判', 'fix')
    s3 = e.create('u', '任务')
    act(e, s3, 'attempt', '错误解释')
    assert s['session_id'] not in [
        p['session_id'] for p in e.traces(s3['session_id'])[0]['context']['prior_task_evidence']
    ]


def test_reply_evidence_is_bound_by_server_not_model():
    from harness.llm.open_model import OpenModel

    m = OpenModel()
    m.chat = lambda *a, **k: '{"outcome":"misconception","hypothesis":"可能误解了规则"}'
    r = m.interpret_reply(
        {
            'question': '你是如何判断的？',
            'student_reply': '我是按自己的猜测判断的。',
            'history': {'other': '不相干的历史'},
        }
    )
    assert r['evidence_quote'] == '我是按自己的猜测判断的。'
    assert r['evidence_binding'] == 'server_current_reply'


def test_duplicate_check_gets_one_bounded_retry(setup):
    e, m = setup
    s = e.create('u', '旧题')
    act(e, s, 'attempt', '正确解释')
    plan = m.plan
    calls = []

    def generate(request, sources, mode, previous):
        result = plan(request, sources, mode, previous)
        calls.append(previous.copy())
        if len(calls) == 1:
            result['question'] = '旧题'
        return result

    m.plan = generate
    r = act(e, s, 'check')
    assert len(calls) == 2 and 'rejected_candidate' in calls[1]
    assert r['session']['task']['question'] != '旧题'
    assert len(e.traces(s['session_id'])[-1]['check_generation_attempts']) == 2


def test_no_pending_answer_or_self_report_credit(setup):
    e, m = setup
    s = e.create('u', '解释原因')
    with pytest.raises(Conflict):
        act(e, s, 'reply', '我明白了')
    assess = m.assess
    m.assess = lambda *a: dict(assess(*a), status='correct')
    r = act(e, s, 'attempt', '我懂了')
    assert r['status'] == 'unknown' and not r['session']['completed']


def test_new_check_context_has_no_previous_help(setup):
    e, _m = setup
    s = e.create('u', '旧题')
    act(e, s, 'hint')
    act(e, s, 'attempt', '正确解释')
    act(e, s, 'check')
    act(e, s, 'attempt', '正确解释')
    ctx = e.traces(s['session_id'])[-1]['context']
    assert ctx['history'] == [] and ctx['prior_task_evidence'] == [] and ctx['hypotheses'] == []


def test_no_material_never_invents_citation(setup):
    e, m = setup
    plan = m.plan
    m.plan = lambda *a: dict(plan(*a), source_quote='杜撰的材料')
    s = e.create('u', '任意任务')
    assert s['task']['source_ids'] == []
    with e.connect() as c:
        _, state = e.load(c, s['session_id'])
    assert state['task']['source_quote'] == ''
    assert state['task']['citation_validation']['discarded_model_quote'] == '杜撰的材料'


def test_probe_cannot_silently_turn_into_explanation(tmp_path):
    m = FakeOpenModel()
    e = OpenEngine(tmp_path / 'open.sqlite', m, policy='open-task-policy-v3')
    s = e.create('u', '任意题目')
    m.candidate = '你的回答不对。正确的方法是先计算。你知道了吗？'
    r = act(e, s, 'attempt', '错误解释')
    assert r['feedback'] == '你得出这个回答时，依据的规则或材料是什么？'
    assert 'probe_must_be_one_question' in e.traces(s['session_id'])[-1]['output_validation']['reasons']


def test_self_correct_does_not_leak_through_writer(setup):
    e, m = setup
    s = e.create('u', '任务')
    act(e, s, 'attempt', '错误解释')
    m.outcome = 'slip'
    m.candidate = '参考结论'
    r = act(e, s, 'reply', '我只是写错了')
    assert r['action'] == 'self_correct' and '参考结论' not in r['feedback']
    assert e.traces(s['session_id'])[-1]['output_validation']['source'] == 'policy_template'
