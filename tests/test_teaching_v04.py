import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_open_tasks import FakeOpenModel, act

from harness.core.gateway import Gateway
from harness.core.open_engine import OpenEngine
from harness.errors import Conflict
from harness.grading.assessment import adjudicate
from harness.resources.toolbox import Toolbox, chunks, retrieve


def assignment(**changes):
    return dict(
        class_id='七年一班',
        course_id='数学',
        concept_id='分配律',
        domain='数学',
        objective='应用分配律并说明依据',
        question='解释分配律',
        rubric='说明每一项都要乘',
        reference='参考结论',
        material='',
        prerequisite='乘法',
        **changes,
    )


@pytest.fixture
def engine(tmp_path):
    return OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())


def assigned(e, learner='学生A', **changes):
    a = e.publish_assignment(assignment(**changes))
    return e.create(learner, assignment_id=a['assignment_id'])


@pytest.mark.parametrize(
    'question,initial,kind,answer',
    [
        ('解方程 2*x+3=7 并解释依据', '2*x+3=7', 'equation', 'x=2'),
        ('将 x*x-1 因式分解', 'x*x-1', 'expression', 'x**2-1'),
    ],
)
def test_partial_tool_evidence_cannot_award_completion(question, initial, kind, answer):
    task = dict(question=question, algebra_initial=initial, algebra_kind=kind)
    v, _ = Toolbox().verify(task, answer)
    assert v['scope'] == 'algebra_equivalence'
    assert adjudicate(task, answer, v, {'status': 'correct', 'confidence': 'high'})[0] == 'incomplete'


def test_no_retrieval_hit_and_source_offsets():
    assert retrieve(chunks('这一段只介绍唐诗格律。'), '牛顿第二定律') == []
    text = '唐诗格律。' * 100 + '\n' + '牛顿第二定律说明力与加速度的关系。'
    sources = chunks(text)
    assert ''.join(x['text'] for x in sources) == text
    assert all(text[x['start'] : x['end']] == x['text'] for x in sources)
    assert '牛顿' in retrieve(sources, '牛顿第二定律')[0]['text']


def test_teacher_task_and_cross_session_memory(engine):
    a = engine.publish_assignment(assignment())
    s = engine.create('A', assignment_id=a['assignment_id'])
    assert engine.model.plans == 0
    assert '参考结论' not in json.dumps(s, ensure_ascii=False)
    act(engine, s, 'attempt', '错误解释')
    changed = assignment()
    changed['objective'] = '把相乘关系用于展开'
    changed['question'] = '另一道分配律题'
    b = engine.publish_assignment(changed)
    s2 = engine.create('A', assignment_id=b['assignment_id'])
    r = act(engine, s2, 'attempt', '错误解释')
    assert r['action'] == 'prerequisite'
    ctx = engine.traces(s2['session_id'])[-1]['context']
    assert ctx['learning_memory'][0]['evidence'][0]['session_id'] == s['session_id']
    assert len(engine.learning_memory('A')) == 1
    assert engine.learning_memory('B') == []
    assert OpenEngine(engine.db, FakeOpenModel()).learning_memory('A') == engine.learning_memory('A')


def test_no_cross_class_memory(engine):
    s = assigned(engine)
    act(engine, s, 'attempt', '错误解释')
    a = assignment()
    a['class_id'] = '八年一班'
    a = engine.publish_assignment(a)
    other = engine.create('学生A', assignment_id=a['assignment_id'])
    act(engine, other, 'attempt', '错误解释')
    ctx = engine.traces(other['session_id'])[-1]['context']
    assert ctx['learning_memory'] == [] and ctx['prior_task_evidence'] == []


def test_correction_invalidates_consumers_transitively(engine):
    s = assigned(engine)
    r = act(engine, s, 'attempt', '错误解释')
    s2 = assigned(engine)
    act(engine, s2, 'attempt', '错误解释')
    s3 = assigned(engine)
    act(engine, s3, 'attempt', '错误解释')
    result = engine.correct(s['session_id'], r['event_id'], 'unknown', '原题有歧义', 'fix')
    assert set(result['invalidated_sessions']) == {x['session_id'] for x in (s, s2, s3)}
    assert engine.learning_memory('学生A')[0]['evidence'] == []
    assert engine.learning_memory('学生A')[0]['state'] == 'needs_recheck'
    with pytest.raises(Conflict):
        act(engine, s2, 'hint')
    assert engine.correct(s['session_id'], r['event_id'], 'unknown', '原题有歧义', 'fix') == result


def test_check_has_no_memory_or_teacher_hint_and_review_due(engine):
    s = assigned(engine, teaching_note='教师指导不可进入独立检查')
    act(engine, s, 'attempt', '正确解释')
    act(engine, s, 'check')
    act(engine, s, 'attempt', '正确解释')
    ctx = engine.traces(s['session_id'])[-1]['context']
    assert ctx['learning_memory'] == [] and ctx['teacher_guidance'] == ''
    assert engine.learning_memory('学生A')[0]['independent_count'] == 1
    from harness.core.memory import summarize

    with engine.connect() as c:
        assert summarize(c, '学生A', now=time.time() + 90000)[0]['review_due']


def test_teacher_explanation_policy_and_review_queue(engine):
    s = assigned(engine, allow_explanation=False)
    with pytest.raises(Conflict):
        act(engine, s, 'explain')
    r = act(engine, s, 'attempt', '错误解释')
    d = engine.dashboard('七年一班')
    assert d['review_queue'][0]['event_id'] == r['event_id']
    engine.correct(s['session_id'], r['event_id'], 'incorrect', '教师确认', 'c')
    assert engine.dashboard('七年一班')['review_queue'] == []


def test_rejected_hints_use_level_templates_and_the_ladder_continues(engine):
    s = assigned(engine)
    engine.model.bad_review = True  # every model draft is rejected
    delivered = [act(engine, s, 'hint')['session']['history'][-1]['delivered_action'] for _ in range(3)]
    assert delivered == ['scaffold_concept_template', 'scaffold_step_template', 'scaffold_example_template']
    assert engine.replay(s['session_id'])['passed']


def test_repeated_generic_fallbacks_still_escalate(engine):
    s = assigned(engine)
    engine.model.bad_review = True
    act(engine, s, 'attempt', '错误解释')
    actions = []
    for _ in range(4):
        session = engine.session(s['session_id'])
        if session['history'][-1].get('action') == 'teacher_review':
            break
        actions.append(act(engine, s, 'reply', '我还是不明白')['action'])
    assert actions[-1] == 'teacher_review'
    assert engine.dashboard()['review_queue'][0]['session_id'] == s['session_id']
    assert engine.replay(s['session_id'])['passed']


def test_no_database_write_lock_during_model_and_stale_snapshot_is_replayed(engine):
    s = assigned(engine)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    calls = []
    old = engine.model.assess

    def slow(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return old(*args)

    engine.model.assess = slow

    def operation():
        try:
            act(engine, s, 'attempt', '正确解释')
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=operation)
    t.start()
    assert entered.wait(3)
    other = OpenEngine(engine.db, FakeOpenModel())
    other.publish_assignment(assignment())  # Must complete while model is blocked.
    release.set()
    t.join(5)
    # The stale computation is re-run on fresh data with the recorded model result.
    assert errors == [] and len(calls) == 1
    assert engine.session(s['session_id'])['version'] == 1
    assert len([t for t in engine.traces(s['session_id']) if t.get('operation') == 'attempt']) == 1


def test_concurrent_students_do_not_conflict(tmp_path):
    class Slow(FakeOpenModel):
        def assess(self, *a):
            time.sleep(0.3)
            return super().assess(*a)

    e = OpenEngine(tmp_path / 'db.sqlite', Slow())
    sessions = [e.create(f'stu{i}', '解释四季成因') for i in range(8)]
    results = []

    def go(s):
        try:
            results.append(act(e, s, 'attempt', '错误解释')['status'])
        except Exception as x:
            results.append(x)

    threads = [threading.Thread(target=go, args=(s,)) for s in sessions]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert results == ['incorrect'] * 8


def test_stale_client_version_still_conflicts(engine):
    s = assigned(engine)
    act(engine, s, 'attempt', '错误解释')
    with pytest.raises(Conflict):
        engine.operation(s['session_id'], 'late', 'attempt', {'text': '正确解释'}, version=0)


def test_authenticated_api_cannot_cross_students_classes_or_use_example_courses(tmp_path, monkeypatch):
    import harness.app as app

    g = Gateway(tmp_path / 'api.sqlite', FakeOpenModel())
    monkeypatch.setattr(app, 'engine', g)
    accounts = tmp_path / 'accounts.json'
    accounts.write_text(
        json.dumps(
            {
                'teacher-token': {'role': 'teacher', 'class_id': '七年一班'},
                'a-token': {'role': 'student', 'learner': 'A', 'class_id': '七年一班'},
                'b-token': {'role': 'student', 'learner': 'B', 'class_id': '七年一班'},
                'other-teacher': {'role': 'teacher', 'class_id': '八年一班'},
            }
        )
    )
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE', str(accounts))

    def headers(token):
        return {'Authorization': 'Bearer ' + token}

    with TestClient(app.app) as c:
        # Pages hold no data and load in classroom mode; every API call needs a token.
        assert c.get('/').status_code == 200
        assert c.get('/api/teacher/dashboard').status_code == 401
        assert c.get('/api/me', headers=headers('a-token')).json() == {
            'mode': 'classroom',
            'role': 'student',
            'class_id': '七年一班',
            'learner': 'A',
        }
        a = c.post('/api/teacher/assignments', json=assignment(), headers=headers('teacher-token'))
        assert a.status_code == 200
        data = {'learner': 'A', 'assignment_id': a.json()['assignment_id']}
        s = c.post('/api/sessions', json=data, headers=headers('a-token')).json()
        path = '/api/sessions/' + s['session_id']
        assert c.get(path, headers=headers('a-token')).status_code == 200
        assert c.get(path, headers=headers('b-token')).status_code == 403
        assert (
            c.get('/api/teacher/sessions/' + s['session_id'] + '/trace', headers=headers('a-token')).status_code == 403
        )
        assert (
            c.get('/api/teacher/sessions/' + s['session_id'] + '/trace', headers=headers('other-teacher')).status_code
            == 403
        )
        assert c.get('/api/teacher/dashboard?class_id=七年一班', headers=headers('teacher-token')).status_code == 200
        assert c.get('/api/teacher/dashboard', headers=headers('teacher-token')).status_code == 403
        assert c.post('/api/sessions', json={**data, 'learner': 'B'}, headers=headers('a-token')).status_code == 403
        assert c.get('/api/teacher/report', headers=headers('teacher-token')).status_code == 403
        assert (
            c.post('/api/sessions', json={'learner': 'A', 'skill': 'k01'}, headers=headers('a-token')).status_code
            == 403
        )


def test_factorized_answer_not_subject_to_simplifier_completion_rule():
    task = {'question': '将 x*x-1 因式分解', 'algebra_initial': 'x*x-1', 'algebra_kind': 'expression'}
    v, _ = Toolbox().verify(task, '(x-1)*(x+1)')
    assert adjudicate(task, '(x-1)*(x+1)', v, {'status': 'correct', 'confidence': 'high'})[0] == 'correct'


def test_interventions_link_followup_without_claiming_causation(engine):
    s = assigned(engine)
    act(engine, s, 'hint')
    r = act(engine, s, 'attempt', '正确解释')
    intervention = engine.learning_memory('学生A')[0]['interventions'][0]
    assert intervention['next_observation']['event_id'] == r['event_id']
    assert intervention['effect'] == 'not_established'


def test_teacher_reference_distinguishes_wrong_form_from_wrong_value():
    task = {'question': '将 x*x-1 因式分解。', 'reference': '(x-1)*(x+1)'}
    v = {'status': 'unknown', 'source': 'model_rubric'}
    model = {'status': 'incorrect', 'confidence': 'high'}
    assert adjudicate(task, 'x**2-1', v, model)[0] == 'incomplete'
    assert adjudicate(task, 'x**2+1', v, model)[0] == 'incorrect'
    assert adjudicate({'question': '解方程并解释依据'}, 'x=99', v, model)[0] == 'incorrect'


def test_scoped_tool_conflict_cannot_be_overridden(engine):
    engine.model.plan = lambda *a: {
        'domain': '数学',
        'objective': '解方程',
        'question': '解方程 2*x+3=7 并解释依据',
        'rubric': '结果和解释均需正确',
        'reference': 'x=2',
        'source_quote': '',
        'prerequisite': '等式性质',
        'algebra_initial': '2*x+3=7',
        'algebra_kind': 'equation',
    }
    engine.model.assess = lambda task, text, *a: {
        'status': 'correct',
        'confidence': 'high',
        'evidence_quote': text,
        'observation': '模型错误地认为正确',
        'hypothesis': '',
    }
    s = engine.create('A', '解方程 2*x+3=7 并解释依据')
    assert act(engine, s, 'attempt', 'x=99')['status'] == 'incorrect'
    assert act(engine, s, 'attempt', 'x=2')['status'] == 'incomplete'


def test_old_aggregate_evidence_still_invalidates_consumers(engine):
    sessions = []
    for _ in range(4):
        s = assigned(engine)
        r = act(engine, s, 'attempt', '正确解释')
        sessions.append((s, r))
    last = assigned(engine)
    act(engine, last, 'hint')
    context = engine.traces(last['session_id'])[-1]['context']
    assert sessions[0][0]['session_id'] in context['memory_dependencies']
    result = engine.correct(
        sessions[0][0]['session_id'], sessions[0][1]['event_id'], 'unknown', '旧证据失效', 'old-fix'
    )
    assert last['session_id'] in result['invalidated_sessions']


def test_image_adapter_preserves_image_and_strips_dependency_metadata():
    from harness.llm.open_model import OpenModel

    m = OpenModel()
    received = []

    def structured(schema, instruction, content, role=None):
        received.append(content)
        return schema(status='unknown', confidence='low', evidence_quote='图', observation='', hypothesis='')

    m.structured = structured
    m.assess(
        {'question': '图题'}, '图', {'image_url': 'data:image/png;base64,a', 'memory_dependencies': ['private-id']}, {}
    )
    assert received[0][1]['type'] == 'image_url'
    assert 'private-id' not in received[0][0]['text']


def test_request_budget_bounds_transport_and_is_restored(monkeypatch):
    from harness.llm.model import ModelClient, ModelUnavailable
    from harness.llm.open_model import OpenModel

    m = OpenModel()
    timeouts = []
    monkeypatch.setattr(ModelClient, 'chat', lambda self, *a, **kw: timeouts.append(kw.get('timeout')) or 'ok')
    with m.budget():
        for _ in range(12):
            assert m.chat([]) == 'ok'
        with pytest.raises(ModelUnavailable):
            m.chat([])
    assert all(0 < x <= 240 for x in timeouts)
    assert m._budget.get() is None


def test_independent_recheck_recovers_without_reviving_invalid_evidence(engine):
    s = assigned(engine)
    r = act(engine, s, 'attempt', '正确解释')
    engine.correct(s['session_id'], r['event_id'], 'unknown', '需重测', 'fix')
    new = assigned(engine)
    act(engine, new, 'attempt', '正确解释')
    act(engine, new, 'check')
    act(engine, new, 'attempt', '正确解释')
    memory = engine.learning_memory('学生A')[0]
    assert memory['state'] == 'independent_observed'
    assert memory['independent_count'] == 1
    assert all(e['session_id'] != s['session_id'] for e in memory['evidence'])


def test_feedback_escalation_takes_priority_over_memory():
    from harness.core.tasks import choose

    assert (
        choose(
            {'phase': 'teach', 'kind': 'attempt', 'status': 'incorrect', 'prior_support_needed': True, 'fallbacks': 2}
        )['action']
        == 'teacher_review'
    )
