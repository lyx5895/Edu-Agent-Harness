"""Accounts and per-record access over HTTP (design doc 10): the server checks every record a request names."""

import random

import pytest
from edu_fake import FakeLLM
from fastapi.testclient import TestClient
from test_edu_workflow import HANDLERS

from edu_app.api import FIELD_KINDS, LIST_KINDS, PATH_KINDS, create_app
from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


@pytest.fixture
def setup(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2, auth=True)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(0))
    app = create_app(ws)
    tokens = {
        name: ws.create_user(name, role)['token']
        for name, role in (('甲老师', 'teacher'), ('乙老师', 'teacher'), ('管理员', 'admin'))
    }

    def client(name=None):
        c = TestClient(app)
        if name:
            c.headers['Authorization'] = f'Bearer {tokens[name]}'
        return c

    return ws, client, tokens


def test_every_route_parameter_has_an_access_rule():
    app = create_app(Workspace(Store(':memory:'), None, load_settings(db=':memory:')))
    for route in app.routes:
        for name in getattr(route, 'param_convertors', {}):
            assert name in PATH_KINDS, f'{route.path} 的参数 {name} 没有权限规则'
    assert {'offering_id', 'class_id', 'assignment_id', 'course_id'} <= set(FIELD_KINDS) and 'project_ids' in LIST_KINDS


def test_teachers_only_reach_their_own_courses(setup):
    ws, client, _tokens = setup
    anon, a, b, admin = client(), client('甲老师'), client('乙老师'), client('管理员')
    assert anon.get('/api/courses').status_code == 401
    assert anon.get('/api/me').json() == {'auth': True, 'user': None}

    crs = a.post('/api/courses', json={'title': '高一数学', 'subject': '数学'}).json()
    cls = a.post('/api/classes', json={'name': '高一（3）班', 'aliases': ['S1', 'S2']}).json()
    off = a.post(f'/api/courses/{crs["id"]}/classes', json={'class_id': cls['id']}).json()
    t = a.post(
        f'/api/courses/{crs["id"]}/tasks',
        json={'kind': 'prep', 'offering_id': off['id'], 'knowledge_module': '对数函数'},
    ).json()
    pid = t['project']['id']
    plan = a.post(f'/api/projects/{pid}/lesson-plan/draft').json()['lesson_plan']

    # Teacher B sees none of it, whatever id is in the path, the query or the body.
    assert b.get('/api/courses').json() == [] and b.get('/api/projects').json() == []
    assert b.get('/api/home').json()['courses'] == [] and b.get('/api/classes').json() == []
    for path in (
        f'/api/courses/{crs["id"]}',
        f'/api/projects/{pid}',
        f'/api/artifacts/{plan["id"]}',
        f'/api/classes/{cls["id"]}',
        f'/api/offerings/{off["id"]}/sessions',
    ):
        assert b.get(path).status_code == 403, path
    assert b.post(f'/api/artifacts/{plan["id"]}/confirm', json={}).status_code == 403
    mine = b.post('/api/courses', json={'title': '我的课', 'subject': '数学'}).json()
    assert b.post(f'/api/courses/{mine["id"]}/classes', json={'class_id': cls['id']}).status_code == 403  # body id
    assert (
        b.post(f'/api/courses/{mine["id"]}/tasks', json={'kind': 'prep', 'offering_id': off['id']}).status_code == 403
    )
    assert b.get('/api/research/summary').status_code == 403  # needs a course it may see
    assert b.get('/api/migration/preview').status_code == 403  # admin only

    # Sharing the course lets B in; the admin sees everything.
    a.post(f'/api/courses/{crs["id"]}/members', json={'name': '乙老师'})
    assert b.get(f'/api/projects/{pid}').status_code == 200
    assert {c['id'] for c in admin.get('/api/courses').json()} == {crs['id'], mine['id']}
    with ws.store.read() as c:
        owner = c.execute('SELECT owner_id FROM class_groups WHERE id=?', (cls['id'],)).fetchone()[0]
        run_user = c.execute('SELECT user_id FROM runs WHERE project_id=?', (pid,)).fetchone()[0]
    assert owner and owner == run_user  # ownership and runs carry the signed-in user


def test_cookie_login_and_unowned_legacy_records(setup):
    ws, client, tokens = setup
    legacy = ws.create_project('旧项目', '数学')  # created outside any request: no owner
    c = client()
    assert c.post('/api/login', json={'token': 'wrong'}).status_code == 401
    assert c.post('/api/login', json={'token': tokens['甲老师']}).status_code == 200
    assert c.get('/api/me').json()['user']['name'] == '甲老师'
    assert c.get(f'/api/projects/{legacy["id"]}').status_code == 403  # only an admin may map old records
    assert client('管理员').get(f'/api/projects/{legacy["id"]}').status_code == 200
    c.post('/api/logout')
    assert c.get('/api/courses').status_code == 401


def test_without_accounts_nothing_changes(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    c = TestClient(create_app(Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings)))
    assert c.get('/api/me').json() == {'auth': False, 'user': None}
    assert c.post('/api/courses', json={'title': 'x', 'subject': '数学'}).status_code == 200


def test_case_review_uses_logged_in_identity_and_server_role(setup):
    _ws, client, _ = setup
    a, b, admin = client('甲老师'), client('乙老师'), client('管理员')
    crs = a.post('/api/courses', json={'title': '审定测试', 'subject': '数学'}).json()
    pid = a.post(f'/api/courses/{crs["id"]}/tasks', json={'kind': 'prep', 'knowledge_module': '对数函数'}).json()[
        'project'
    ]['id']
    plan = a.post(f'/api/projects/{pid}/lesson-plan/draft').json()['lesson_plan']
    a.post(f'/api/artifacts/{plan["id"]}/confirm', json={})
    a.post('/api/research/cases/build', params={'course_id': crs['id']})
    case = a.get('/api/research/cases', params={'course_id': crs['id']}).json()[0]
    url = f'/api/research/cases/{case["id"]}/review?course_id={crs["id"]}'
    body = {'case_version': case['version'], 'reviewer': '虚构审定人', 'verdict': 'approve', 'standard': '标准'}
    assert a.post(url, json={**body, 'role': 'adjudicator'}).status_code == 403
    assert a.post(url, json=body).status_code == 409
    a.post(f'/api/courses/{crs["id"]}/members', json={'name': '乙老师'})
    assert b.post(url, json=body).status_code == 200
    assert b.post(url, json={**body, 'reviewer': '另一个假名'}).status_code == 409
    stored = b.get('/api/research/cases', params={'course_id': crs['id']}).json()[0]['reviews'][0]
    assert stored['reviewer'] == b.get('/api/me').json()['user']['id']
    assert admin.post(url, json={**body, 'role': 'adjudicator'}).status_code == 200

    # A contributor to an earlier version is still not an independent reviewer.
    edit_b = b.put(f'/api/artifacts/{plan["id"]}', json={'content': {'notes': '乙修改'}}).json()
    edit_a = a.put(f'/api/artifacts/{edit_b["id"]}', json={'content': {'notes': '甲再修改'}}).json()
    a.post(f'/api/artifacts/{edit_a["id"]}/confirm', json={})
    a.post('/api/research/cases/build', params={'course_id': crs['id']})
    latest = a.get('/api/research/cases', params={'course_id': crs['id']}).json()[0]
    assert latest['version'] == case['version'] + 1
    assert b.post(url, json={**body, 'case_version': latest['version']}).status_code == 409


def test_dataset_export_retry_returns_same_export(setup):
    ws, client, _ = setup
    admin = client('管理员')
    headers = {'Idempotency-Key': 'dataset-retry'}
    first = admin.post('/api/datasets/export', headers=headers)
    second = admin.post('/api/datasets/export', headers=headers)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    with ws.store.read() as c:
        assert c.execute('SELECT COUNT(*) FROM dataset_exports').fetchone()[0] == 1
