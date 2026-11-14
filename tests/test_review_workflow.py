import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_open_tasks import FakeOpenModel, act

from harness.core.open_engine import OpenEngine


def test_review_uses_historical_task_and_rejects_unrelated_event(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('学生', '解释月相变化')
    original = s['task']['question']
    turn = act(e, s, 'attempt', '正确解释')
    new = act(e, turn['session'], 'check')['session']
    assert new['task']['question'] != original
    detail = e.review_detail(s['session_id'], turn['event_id'])
    assert detail['task']['question'] == original and detail['text'] == '正确解释'
    assert detail['task']['reference'] == '参考结论' and detail['can_confirm']
    other = e.create('另一学生', '解释季节变化')
    with pytest.raises(ValueError):
        e.review_detail(other['session_id'], turn['event_id'])
    with pytest.raises(ValueError):
        e.review_detail(s['session_id'], 999999)


def test_review_original_image_is_bound_to_attempt(tmp_path):
    e = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    s = e.create('学生', '解释月相变化')
    raw = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(raw, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(raw.getvalue()).decode()
    uploaded = e.operation(s['session_id'], 'photo', 'image', {'image_url': url})['session']
    turn = act(e, uploaded, 'confirm', '错误解释')
    d = e.review_detail(s['session_id'], turn['event_id'])
    assert d['image_event_id'] is not None
    assert e.image_event(s['session_id'], d['image_event_id'])['purpose'] == 'work'


def test_review_detail_endpoint_enforces_teacher_and_class(tmp_path, monkeypatch):
    from harness.app import create_app
    from harness.core.gateway import Gateway

    gateway = Gateway(tmp_path / 'db.sqlite', FakeOpenModel())
    aid = gateway.open.publish_assignment(
        dict(
            class_id='C',
            course_id='科学',
            concept_id='月相',
            domain='科学',
            objective='解释月相',
            question='月相为何变化',
            rubric='解释原因',
            reference='参考结论',
        )
    )['assignment_id']
    s = gateway.create_task(learner='A', assignment_id=aid)
    turn = act(gateway.open, s, 'attempt', '错误解释')
    config = tmp_path / 'accounts.json'
    config.write_text(
        json.dumps(
            {
                'teacher': {'role': 'teacher', 'class_id': 'C'},
                'other': {'role': 'teacher', 'class_id': 'D'},
                'student': {'role': 'student', 'class_id': 'C', 'learner': 'A'},
            }
        )
    )
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE', str(config))
    with TestClient(create_app(lambda: gateway, with_ui=False)) as client:
        path = f'/api/teacher/tasks/{s["session_id"]}/attempts/{turn["event_id"]}'
        assert client.get(path, headers={'Authorization': 'Bearer teacher'}).json()['task']['reference'] == '参考结论'
        for token in ['other', 'student']:
            assert client.get(path, headers={'Authorization': 'Bearer ' + token}).status_code == 403
        assert client.get(path).status_code == 401


def test_review_api_confirms_once_and_removes_attempt_from_queue(tmp_path):
    from harness.app import create_app
    from harness.core.gateway import Gateway

    gateway = Gateway(tmp_path / 'db.sqlite', FakeOpenModel())
    s = gateway.create_task(learner='学生', request='解释月相变化')
    turn = act(gateway.open, s, 'attempt', '错误解释')
    base = f'/api/teacher/tasks/{s["session_id"]}/attempts/{turn["event_id"]}'
    with TestClient(create_app(lambda: gateway, with_ui=False)) as client:
        assert client.get(base).json()['can_confirm']
        assert client.post(base + '/reviews', json={'reason': '', 'request_id': 'r0'}).status_code == 422
        bad = {'status': 'wrong', 'reason': 'x', 'request_id': 'c0'}
        assert client.post(base + '/corrections', json=bad).status_code == 400
        confirmed = client.post(base + '/reviews', json={'reason': '已对照原题核对错误依据', 'request_id': 'r1'})
        assert confirmed.status_code == 200
        detail = client.get(base).json()
        assert detail['decisions'][-1]['kind'] == 'review_confirmed' and not detail['can_correct']
        queue = client.get('/api/teacher/dashboard').json()['review_queue']
        assert not any(r['event_id'] == turn['event_id'] for r in queue)
    assert not gateway.session(s['session_id'])['needs_review']
