import json
import time

import pytest
from fastapi.testclient import TestClient
from test_open_tasks import FakeOpenModel, act

from harness.core.gateway import Gateway
from harness.core.open_engine import OpenEngine
from harness.errors import Conflict
from harness.grading.criteria import bind_algebra, tool_grade


class RubricModel(FakeOpenModel):
    def assess(self, task, text, context, verification):
        result = super().assess(task, text, context, verification)
        if text == 'x=2；等式两边同时减3，再同时除以2。':
            result['status'] = 'correct'
        return result

    def assess_criterion(self, task, criterion, text, context):
        return {
            'status': 'correct' if '两边' in text else 'incomplete',
            'confidence': 'high',
            'evidence_quote': text,
            'observation': '按该项要求检查',
            'hypothesis': '',
        }


@pytest.fixture
def engine(tmp_path):
    return OpenEngine(tmp_path / 'db.sqlite', RubricModel())


def publish(e, concept='linear', question='解方程 2*x+3=7', reference='x=2', **extra):
    return e.publish_assignment(
        dict(
            class_id='C',
            course_id='M',
            concept_id=concept,
            domain='数学',
            objective=concept,
            question=question,
            rubric='结果正确；有解释要求时需要解释',
            reference=reference,
            **extra,
        )
    )['assignment_id']


def start(e, aid, learner='A'):
    return e.create(learner, assignment_id=aid)


def task(e, aid, kind='review', learners=None, **extra):
    return e.assign_followups(
        dict(
            class_id='C',
            assignment_id=aid,
            kind=kind,
            learners=learners or ['A'],
            due_at=time.time() - 1,
            reason='教师安排',
            **extra,
        ),
        str(time.time_ns()),
    )


def test_global_memory_order_and_intervention_followup(engine):
    aid = publish(engine, question='解释规律', reference='正确解释')
    a = start(engine, aid)
    b = start(engine, aid)
    wrong = act(engine, b, 'attempt', '错误解释')
    correct = act(engine, a, 'attempt', '正确解释')
    m = engine.learning_memory('A')[0]
    assert m['state'] == 'practice_complete'
    assert [x['event_id'] for x in m['evidence']] == [wrong['event_id'], correct['event_id']]
    assert m['latest_observation']['event_id'] == correct['event_id']
    assert m['interventions'][0]['next_observation']['event_id'] == correct['event_id']


def test_teacher_equation_binding_and_wrong_math_override(engine):
    aid = publish(engine)
    s = start(engine, aid)
    with engine.connect() as c:
        _, state = engine.load(c, s['session_id'])
    assert state['task']['algebra_initial'] == '2*x+3=7'
    assessment = engine.model.assess
    engine.model.assess = lambda *a: dict(assessment(*a), status='correct', confidence='high')
    assert act(engine, s, 'attempt', 'x=99')['status'] == 'incorrect'
    assert engine.dashboard('C')['review_queue'][0]['priority'] == 'urgent'
    with pytest.raises(ValueError):
        publish(engine, algebra_initial='x=9', algebra_kind='equation')
    assert bind_algebra('解 x=2 和 x=3')['algebra_kind'] == 'none'


def test_dimension_results_are_independent_and_not_reference_leak(engine):
    aid = publish(
        engine,
        question='解方程 2*x+3=7 并解释依据',
        criteria=[
            {'id': 'value', 'kind': 'algebra', 'description': '方程求解正确'},
            {'id': 'reason', 'kind': 'explanation', 'description': '解释等式性质'},
        ],
    )
    s = start(engine, aid)
    r = act(engine, s, 'attempt', 'x=2')
    assert r['status'] == 'incomplete'
    assert [x['status'] for x in r['criteria_results']] == ['correct', 'incomplete']
    assert r['criteria_results'][0]['source'] == 'tool'
    assert 'expected' not in json.dumps(s)
    r = act(engine, s, 'attempt', 'x=2；等式两边同时减3，再同时除以2。')
    # Algebra with prose abstains; per-dimension model can evaluate this item.
    assert r['status'] == 'correct'
    assert engine.learning_memory('A')[0]['evidence'][-1]['criteria']


def test_numeric_unit_and_factorization_dimensions():
    def c(kind, expected):
        return dict(id='c', description='测试', kind=kind, expected=expected)

    assert tool_grade(c('numeric', '4'), {}, '4 m/s²')['status'] == 'correct'
    assert tool_grade(c('unit', 'm/s²'), {}, '4')['status'] == 'incomplete'
    assert tool_grade(c('unit', 'm/s²'), {}, '4 kg')['status'] == 'incorrect'
    assert tool_grade(c('factorization', '(x-1)*(x+1)'), {}, '(x*x)-1')['status'] == 'incomplete'
    assert tool_grade(c('factorization', '(x-1)*(x+1)'), {}, '(x-1)*(x+1)')['status'] == 'correct'
    assert tool_grade(c('factorization', '(x-1)*(x+1)'), {}, 'x*x+1')['status'] == 'incorrect'


def test_publish_rejects_duplicate_criteria_and_bad_reference(engine):
    with pytest.raises(ValueError):
        publish(engine, criteria=[{'id': 'x', 'description': 'x'}, {'id': 'x', 'description': 'y'}])
    with pytest.raises(ValueError):
        publish(engine, reference='x=99', criteria=[{'id': 'x', 'kind': 'algebra', 'description': '求解'}])


def test_curriculum_rejects_cycles_and_wrong_scope(engine):
    a = publish(engine, 'a')
    b = publish(engine, 'b')
    nodes = [
        dict(concept_id='a', title='A', assignment_id=a, prerequisite_ids=['b']),
        dict(concept_id='b', title='B', assignment_id=b, prerequisite_ids=['a']),
    ]
    with pytest.raises(ValueError):
        engine.publish_path(dict(class_id='C', course_id='M', title='path', nodes=nodes))
    nodes[0]['prerequisite_ids'] = []
    with pytest.raises(ValueError):
        engine.publish_path(dict(class_id='other', course_id='M', title='path', nodes=nodes))
    path = engine.publish_path(dict(class_id='C', course_id='M', title='path', nodes=list(reversed(nodes))))
    n = engine.next_step(path['path_id'], 'A')
    assert n['action'] == 'diagnose' and n['concept_id'] == 'a'


def test_review_lifecycle_and_path_progress(engine):
    a = publish(engine, 'a', criteria=[{'id': 'math', 'kind': 'algebra', 'description': '求解方程'}])
    b = publish(engine, 'b', question='解方程 3*x+1=10', reference='x=3')
    path = engine.publish_path(
        dict(
            class_id='C',
            course_id='M',
            title='path',
            nodes=[
                dict(concept_id='a', title='A', assignment_id=a),
                dict(concept_id='b', title='B', assignment_id=b, prerequisite_ids=['a']),
            ],
        )
    )
    f = task(engine, a)[0]
    assert engine.next_step(path['path_id'], 'A')['followup_id'] == f['id']
    s = engine.create('A', followup_id=f['id'])
    assert s['task']['phase'] == 'check'
    assert engine.create('A', followup_id=f['id'])['session_id'] == s['session_id']
    with pytest.raises(Conflict):
        act(engine, s, 'hint')
    r = act(engine, s, 'attempt', 'x=2')
    # Whole-task completeness now depends on the model even if dimensions use tools.
    assert r['evidence'] == 'independent_model_assessed'
    assert engine.followups('C', 'A')[0]['status'] == 'completed'
    assert engine.next_step(path['path_id'], 'A')['concept_id'] == 'b'
    engine.correct(s['session_id'], r['event_id'], 'unknown', '参考需复查', 'fix')
    assert engine.followups('C', 'A')[0]['status'] == 'needs_recheck'
    assert engine.next_step(path['path_id'], 'A')['concept_id'] == 'a'


def test_followup_batch_atomicity_idempotency_postpone_cancel(engine):
    aid = publish(engine)
    body = dict(
        class_id='C', assignment_id=aid, learners=['A', 'B'], kind='remediation', due_at=time.time(), reason='补救'
    )
    batch = engine.assign_followups(body, 'key')
    assert engine.assign_followups(body, 'key') == batch
    assert len(engine.followups('C')) == 2
    f = batch[0]
    updated = engine.revise_followup(f['id'], 'postpone', time.time() + 3600, '延期', 'rev')
    assert updated['due_at'] > f['due_at']
    engine.revise_followup(f['id'], 'cancel', None, '撤销', 'cancel')
    with pytest.raises(Conflict):
        engine.create('A', followup_id=f['id'])
    with pytest.raises(ValueError):
        engine.create('A', followup_id=batch[1]['id'])
    start(engine, aid, 'D')
    with pytest.raises(ValueError):
        task(engine, aid, learners=['E', 'D'])
    assert engine.followups('C', 'E') == []


def test_guided_remediation_does_not_count_as_completed(engine):
    aid = publish(engine, question='解释概念', reference='正确解释')
    f = task(engine, aid, 'remediation')[0]
    s = engine.create('A', followup_id=f['id'])
    act(engine, s, 'hint')
    act(engine, s, 'attempt', '正确解释')
    assert engine.followups('C', 'A')[0]['status'] == 'active'
    act(engine, s, 'check')
    act(engine, s, 'attempt', '正确解释')
    assert engine.followups('C', 'A')[0]['status'] == 'completed'


def test_new_routes_scope_and_followup_owner(tmp_path, monkeypatch):
    import harness.app as app

    g = Gateway(tmp_path / 'api.sqlite', RubricModel())
    monkeypatch.setattr(app, 'engine', g)
    file = tmp_path / 'accounts.json'
    file.write_text(
        json.dumps(
            {
                't': {'role': 'teacher', 'class_id': 'C'},
                'a': {'role': 'student', 'class_id': 'C', 'learner': 'A'},
                'b': {'role': 'student', 'class_id': 'C', 'learner': 'B'},
                'o': {'role': 'teacher', 'class_id': 'D'},
            }
        )
    )
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE', str(file))

    def h(x):
        return {'Authorization': 'Bearer ' + x}

    aid = publish(g.open)
    with TestClient(app.app) as c:
        body = {
            'class_id': 'C',
            'assignment_id': aid,
            'learners': ['A'],
            'kind': 'review',
            'due_at': 0,
            'reason': '复习',
            'request_id': 'k',
        }
        assert c.post('/api/teacher/followups', json=body, headers=h('a')).status_code == 403
        f = c.post('/api/teacher/followups', json=body, headers=h('t')).json()[0]
        assert c.get('/api/learning/followups?class_id=C&learner=A', headers=h('b')).status_code == 403
        assert c.get('/api/learning/followups?class_id=C&learner=A', headers=h('a')).status_code == 200
        assert c.post('/api/sessions', json={'learner': 'A', 'followup_id': f['id']}, headers=h('a')).status_code == 200
        assert c.post('/api/sessions', json={'learner': 'B', 'followup_id': f['id']}, headers=h('b')).status_code == 403
        assert (
            c.patch(
                '/api/teacher/followups/' + f['id'],
                json={'action': 'cancel', 'reason': '取消', 'request_id': 'r'},
                headers=h('o'),
            ).status_code
            == 403
        )
        path = c.post(
            '/api/teacher/paths',
            json={
                'class_id': 'C',
                'course_id': 'M',
                'title': '课程',
                'nodes': [{'concept_id': 'linear', 'title': '方程', 'assignment_id': aid}],
            },
            headers=h('t'),
        ).json()
        assert (
            c.get('/api/learning/next', params={'path_id': path['path_id'], 'learner': 'A'}, headers=h('a')).status_code
            == 200
        )
        assert (
            c.get('/api/learning/next', params={'path_id': path['path_id'], 'learner': 'A'}, headers=h('b')).status_code
            == 403
        )


def test_teacher_confirmation_does_not_invalidate_memory(engine):
    aid = publish(engine, question='解释规则', reference='正确解释')
    s = start(engine, aid)
    r = act(engine, s, 'attempt', '错误解释')
    assert engine.dashboard('C')['review_queue']
    result = engine.confirm_review(s['session_id'], r['event_id'], '核对原始作答后确认', 'review')
    assert result['status'] == 'incorrect'
    assert not engine.session(s['session_id'])['needs_review']
    assert engine.dashboard('C')['review_queue'] == []
    assert engine.learning_memory('A')[0]['evidence']


def test_due_teacher_task_overrides_stale_pending_work(engine):
    old = publish(engine, 'a', question='解释概念', reference='正确解释')
    start(engine, old)
    fresh = publish(engine, 'a', question='解释另一情境', reference='正确解释')
    path = engine.publish_path(
        dict(class_id='C', course_id='M', title='路径', nodes=[dict(concept_id='a', title='A', assignment_id=old)])
    )
    f = task(engine, fresh)[0]
    assert engine.next_step(path['path_id'], 'A')['followup_id'] == f['id']


def test_dimension_private_observations_are_teacher_only(engine):
    aid = publish(
        engine,
        question='解释规律',
        reference='保密参考',
        criteria=[{'id': 'reason', 'kind': 'model', 'description': '解释依据'}],
    )
    engine.model.assess_criterion = lambda *a: dict(
        status='correct', confidence='high', evidence_quote='我的解释', observation='保密参考内容', hypothesis=''
    )
    s = start(engine, aid)
    r = act(engine, s, 'attempt', '我的解释')
    assert '保密参考内容' not in json.dumps(r, ensure_ascii=False)
    with engine.connect() as c:
        payload = json.loads(
            c.execute('SELECT payload FROM learning_events WHERE id=?', (r['event_id'],)).fetchone()[0]
        )
    assert payload['criteria_results'][0]['reason'] == '保密参考内容'


def test_dimension_invalid_quote_abstains(engine):
    original = engine.model.assess
    engine.model.assess = lambda *a: dict(original(*a), status='correct')
    aid = publish(
        engine,
        question='解释规律',
        reference='参考',
        criteria=[{'id': 'reason', 'kind': 'model', 'description': '解释依据'}],
    )
    engine.model.assess_criterion = lambda *a: dict(
        status='correct', confidence='high', evidence_quote='学生没有说过', observation='正确', hypothesis=''
    )
    r = act(engine, start(engine, aid), 'attempt', '我的解释')
    assert r['status'] == 'unknown'


def test_old_independent_success_does_not_hide_new_difficulty(engine):
    a = publish(engine, 'a', criteria=[{'id': 'math', 'kind': 'algebra', 'description': '求解'}])
    b = publish(engine, 'b', question='解方程 3*x+1=10', reference='x=3')
    path = engine.publish_path(
        dict(
            class_id='C',
            course_id='M',
            title='path',
            nodes=[
                dict(concept_id='a', title='A', assignment_id=a),
                dict(concept_id='b', title='B', assignment_id=b, prerequisite_ids=['a']),
            ],
        )
    )
    f = task(engine, a)[0]
    s = engine.create('A', followup_id=f['id'])
    act(engine, s, 'attempt', 'x=2')
    assert engine.learning_memory('A')[0]['independent_current']
    later = start(engine, a)
    act(engine, later, 'attempt', 'x=99')
    act(engine, later, 'hint')
    act(engine, later, 'attempt', 'x=2')
    assert not engine.learning_memory('A')[0]['independent_current']
    assert engine.next_step(path['path_id'], 'A')['concept_id'] == 'a'
