"""HTTP layer of the teacher app: routes, error codes and the static page."""

import random

import pytest
from edu_fake import FakeLLM
from fastapi.testclient import TestClient
from test_edu_workflow import HANDLERS

from edu_app.api import create_app
from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


@pytest.fixture
def client(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(0))
    return TestClient(create_app(ws))


def test_teacher_flow_over_http(client):
    meta = client.get('/api/meta').json()
    assert len(meta['subjects']) == 10 and meta['model_ready']
    p = client.post('/api/projects', json={'title': '高一数学｜对数函数', 'subject': 'math', 'grade': '高一'}).json()
    pid = p['id']
    assert (
        client.post(f'/api/projects/{pid}/materials', json={'text': '对数函数的定义域是 (0,+∞)。'}).status_code == 200
    )
    up = client.post(
        f'/api/projects/{pid}/materials/upload',
        files={'file': ('讲义.md', '# 讲义\n真数大于零'.encode(), 'text/markdown')},
    )
    assert up.status_code == 200 and up.json()['filename'] == '讲义.md'

    drafted = client.post(f'/api/projects/{pid}/lesson-plan/draft').json()
    assert drafted['context_note'] == '已参考：项目背景、材料 2 份、课程标准 3 条'
    plan = drafted['lesson_plan']
    assert plan['content']['objectives'][0]['id'] == 'O1'
    assert client.post(f'/api/artifacts/{plan["id"]}/confirm', json={}).json()['status'] == 'confirmed'

    gen = client.post(
        f'/api/projects/{pid}/questions/generate', json={'types': ['calculation'], 'count': 1, 'ab': False}
    ).json()
    q = gen['candidates'][0]['questions'][0]
    client.post(f'/api/artifacts/{q["id"]}/confirm', json={})
    [listed] = client.get(f'/api/projects/{pid}/questions?round=1').json()
    assert listed['status'] == 'confirmed' and listed['rubric']['status'] == 'confirmed'

    client.post(f'/api/projects/{pid}/learners', json={'aliases': ['S01', 'S02']})
    imp = client.post(
        f'/api/projects/{pid}/submissions', json={'csv': 'S01,1,定义域 x>1，所以 1<x<9\nS02,1,x-1<8，x<9'}
    ).json()
    assert imp['created'] == 2
    graded = client.post(f'/api/projects/{pid}/grade', json={'round': 1}).json()
    assert graded['graded'] == 2 and graded['review_queue'] == 0
    assert client.post(f'/api/projects/{pid}/gradings/confirm', json={'round': 1}).json() == {'confirmed': 2, 'held': 0}
    analysis = client.post(f'/api/projects/{pid}/analyze', json={'round': 1}).json()
    assert analysis['patterns'] and '批改 2 份' in analysis['context_note']

    overview = client.get(f'/api/projects/{pid}').json()
    assert overview['counts']['grading'] == {'total': 2, 'confirmed': 2}
    export = client.get(f'/api/projects/{pid}/export')
    assert export.status_code == 200 and 'attachment' in export.headers['content-disposition']
    assert export.json()['manifest']['counts']['material_families'] == 2


def test_error_codes(client):
    assert client.get('/api/projects/nope').status_code == 404
    assert client.post('/api/projects', json={'title': 'x', 'subject': '../math'}).status_code == 400
    pid = client.post('/api/projects', json={'title': 'x', 'subject': 'history'}).json()['id']
    r = client.post(f'/api/projects/{pid}/questions/generate', json={})
    assert r.status_code == 409 and '主题' in r.json()['detail']  # nothing to infer objectives from yet
    upd = client.patch(f'/api/projects/{pid}', json={'knowledge_module': '辛亥革命', 'lesson': '第1课时'}).json()
    assert upd['knowledge_module'] == '辛亥革命' and upd['lesson'] == '第1课时'
    assert client.get('/api/nothing').status_code == 404


def test_static_page(client):
    page = client.get('/')
    assert page.status_code == 200 and 'Edu-Agent' in page.text
    assert page.headers['cache-control'] == 'no-cache'
    assert '/static/app.js?v=' in page.text and '/static/app.css?v=' in page.text  # content-versioned assets
    js = client.get('/static/app.js')
    assert js.status_code == 200 and js.headers['cache-control'] == 'no-cache'


def test_next_lesson_route_needs_learning_status(client):
    pid = client.post('/api/projects', json={'title': '历史｜辛亥革命', 'subject': 'history'}).json()['id']
    r = client.post(f'/api/projects/{pid}/next-lesson', json={})
    assert r.status_code == 409 and '学情分析' in r.json()['detail']
    assert client.get(f'/api/projects/{pid}').json()['next_lesson'] is None


def test_curriculum_routes(client):
    pid = client.post(
        '/api/projects', json={'title': '高一数学', 'subject': 'math', 'knowledge_module': '对数函数'}
    ).json()['id']
    sug = client.get(f'/api/projects/{pid}/curriculum').json()
    assert sug['available'] and sug['items'] and sug['selected']
    found = client.get(f'/api/projects/{pid}/curriculum', params={'q': '基本不等式'}).json()
    assert found['items'][0]['id'] == 'math.B1.3.2'
    upd = client.patch(
        f'/api/projects/{pid}', json={'lesson_scope': '图象与性质', 'curriculum_ids': ['math.B2.2.3.2']}
    ).json()
    assert upd['lesson_scope'] == '图象与性质' and upd['curriculum']['ids'] == ['math.B2.2.3.2']


def test_question_review_routes(client):
    pid = client.post(
        '/api/projects', json={'title': '高一数学', 'subject': 'math', 'knowledge_module': '对数函数'}
    ).json()['id']
    plan = client.post(f'/api/projects/{pid}/lesson-plan/draft').json()['lesson_plan']
    client.post(f'/api/artifacts/{plan["id"]}/confirm', json={})
    gen = client.post(
        f'/api/projects/{pid}/questions/generate', json={'types': ['calculation'], 'count': 1, 'ab': False}
    ).json()
    q = gen['candidates'][0]['questions'][0]
    cov = client.get(f'/api/projects/{pid}/coverage').json()
    assert cov['objectives'][0]['total'] == 1
    r = client.post(f'/api/questions/{q["id"]}/adopt-solver')
    assert r.status_code == 200 and r.json()['version'] == 2
    bad = client.post(f'/api/questions/{q["id"]}/regenerate', json={'difficulty': '太难'})
    assert bad.status_code == 422


def _app(tmp_path, **overrides):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2, **overrides)
    return create_app(Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(0)))


def test_only_local_clients_by_default(tmp_path):
    app = _app(tmp_path)
    assert TestClient(app, client=('127.0.0.1', 50000)).get('/api/projects').status_code == 200
    assert TestClient(app, client=('::1', 50000)).get('/api/projects').status_code == 200
    remote = TestClient(app, client=('203.0.113.7', 50000))
    assert remote.get('/api/projects').status_code == 403
    assert remote.get('/').status_code == 403


def test_remote_clients_when_allowed(tmp_path):
    app = _app(tmp_path, allow_remote=True)
    assert TestClient(app, client=('203.0.113.7', 50000)).get('/api/projects').status_code == 200


def test_photo_answers_over_http(tmp_path):
    from test_edu_workflow import _photo, setup_round_one

    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(1))
    p, _q = setup_round_one(ws)
    pid = p['id']
    client = TestClient(create_app(ws))
    r = client.post(
        f'/api/projects/{pid}/photo-answers/transcribe',
        data={'learner': '甲', 'round': '1'},
        files=[('files', ('p1.png', _photo(), 'image/png'))],
    )
    assert r.status_code == 200, r.text
    draft = r.json()
    got = client.get(f'/api/photos/{draft["photos"][0]}')
    assert got.status_code == 200 and got.headers['content-type'] == 'image/jpeg'
    assert client.get('/api/photos/not-a-photo.jpeg').status_code == 400
    answers = [{'question_no': 1, 'text': '定义域 x>1，所以 1<x<9'}]
    r = client.post(
        f'/api/projects/{pid}/photo-answers', json={'learner': '甲', 'answers': answers, 'photos': draft['photos']}
    )
    assert r.status_code == 200 and r.json()['created'] == 1
    bad = client.post(
        f'/api/projects/{pid}/photo-answers/transcribe',
        data={'learner': '甲'},
        files=[('files', ('x.png', b'nope', 'image/png'))],
    )
    assert bad.status_code == 400


def test_course_workspace_over_http(client):
    crs = client.post('/api/courses', json={'title': '高一数学·上学期', 'subject': '数学'}).json()
    cls = client.post('/api/classes', json={'name': '高一（3）班', 'aliases': ['甲', '乙']}).json()
    off = client.post(f'/api/courses/{crs["id"]}/classes', json={'class_id': cls['id']}).json()
    assert client.get('/api/home').json()['offerings'][0]['class_name'] == '高一（3）班'

    key = {'Idempotency-Key': 'start-1'}
    body = {'kind': 'prep', 'offering_id': off['id'], 'knowledge_module': '对数函数', 'lesson': '第2课时'}
    started = client.post(f'/api/courses/{crs["id"]}/tasks', json=body, headers=key).json()
    again = client.post(f'/api/courses/{crs["id"]}/tasks', json=body, headers=key).json()
    assert started['task']['id'] == again['task']['id'] and len(client.get('/api/tasks').json()) == 1
    pid = started['project']['id']

    client.post(f'/api/projects/{pid}/materials', json={'text': '对数函数的定义域是 (0,+∞)。'})
    plan = client.post(f'/api/projects/{pid}/lesson-plan/draft').json()['lesson_plan']
    client.post(f'/api/artifacts/{plan["id"]}/confirm', json={})
    [session] = client.get(f'/api/offerings/{off["id"]}/sessions').json()
    assert session['status'] == 'planned'
    gen = client.post(
        f'/api/projects/{pid}/questions/generate', json={'types': ['calculation'], 'count': 1, 'ab': False}
    ).json()
    q = gen['candidates'][0]['questions'][0]
    client.post(f'/api/artifacts/{q["id"]}/confirm', json={})

    a = client.post(f'/api/projects/{pid}/assignments', json={'offering_id': off['id']}).json()
    client.post(f'/api/projects/{pid}/submissions', json={'csv': '甲,1,x>1 且 1<x<9', 'assignment_id': a['id']})
    client.post(f'/api/projects/{pid}/grade', json={'assignment_id': a['id']})
    assert len(client.get(f'/api/projects/{pid}/gradings?assignment_id={a["id"]}').json()) == 1
    assert client.get(f'/api/projects/{pid}/gradings').json() == []
    ov = client.get(f'/api/projects/{pid}').json()
    assert ov['assignment_id'] == a['id'] and ov['assignments'][0]['class_name'] == '高一（3）班'
    impact = client.get(f'/api/assignments/{a["id"]}/correction?question_lineage={q["lineage_id"]}').json()
    assert impact['gradings'] == 1 and impact['other_assignments'] == []
    assert client.patch(f'/api/sessions/{session["id"]}', json={'status': 'taught'}).json()['status'] == 'taught'
    assert client.get('/api/migration/preview').json()['groups'] == []


def test_topics_explain_derive_and_status_prefs_over_http(client):
    crs = client.post('/api/courses', json={'title': '高一数学', 'subject': 'math'}).json()
    unit = client.post(f'/api/courses/{crs["id"]}/units', json={'title': '对数函数'}).json()
    t = client.post(f'/api/courses/{crs["id"]}/tasks', json={'kind': 'explain', 'knowledge_module': '对数函数'}).json()
    assert t['project']['unit_id'] == unit['id']
    assert client.get(f'/api/units/{unit["id"]}').json()['unit']['title'] == '对数函数'
    assert client.get(f'/api/projects/{t["project"]["id"]}/class-status').json() is None  # no class: nothing to send
    assert client.get(f'/api/projects/{t["project"]["id"]}/explanations').json() == []
    assert client.post(f'/api/projects/{t["project"]["id"]}/explain', json={'question': ''}).status_code == 422
    r = client.post(f'/api/projects/{t["project"]["id"]}/derive', json={'offering_id': 'off_missing'})
    assert r.status_code == 404


def test_trial_report_and_feedback_over_http(client):
    cid = client.post('/api/courses', json={'title': '高一数学·上学期', 'subject': '数学'}).json()['id']
    report = client.get('/api/trial/report', params={'course_id': cid}).json()
    assert report['summary']['tasks'] == 0 and report['targets']['cross_class_errors']
    assert client.post('/api/trial/feedback', json={'willing': True, 'note': 'ok'}).json()['willing'] is True
    again = client.get('/api/trial/report', params={'course_id': cid}).json()
    assert again['summary']['willingness']['willing'] == 1
