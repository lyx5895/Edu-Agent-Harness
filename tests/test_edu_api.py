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

    drafted = client.post(f'/api/projects/{pid}/objectives/draft').json()
    assert drafted['context_note'] == '已参考：项目背景、材料 2 份'
    obj = drafted['objectives'][0]
    assert client.post(f'/api/artifacts/{obj["id"]}/confirm', json={}).json()['status'] == 'confirmed'

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
    assert client.post(f'/api/projects/{pid}/gradings/confirm', json={'round': 1}).json() == {'confirmed': 2}
    analysis = client.post(f'/api/projects/{pid}/analyze', json={'round': 1}).json()
    assert analysis['patterns'] and '批改 2 份' in analysis['context_note']

    overview = client.get(f'/api/projects/{pid}').json()
    assert overview['counts']['grading'] == {'total': 2, 'confirmed': 2}
    export = client.get(f'/api/projects/{pid}/export')
    assert export.status_code == 200 and 'attachment' in export.headers['content-disposition']
    assert export.json()['manifest']['counts']['material_families'] == 2


def test_error_codes(client):
    assert client.get('/api/projects/nope').status_code == 404
    assert client.post('/api/projects', json={'title': 'x', 'subject': '美术'}).status_code == 400
    pid = client.post('/api/projects', json={'title': 'x', 'subject': 'history'}).json()['id']
    r = client.post(f'/api/projects/{pid}/questions/generate', json={})
    assert r.status_code == 409 and '教学目标' in r.json()['detail']
    assert client.get('/api/nothing').status_code == 404


def test_static_page(client):
    page = client.get('/')
    assert page.status_code == 200 and 'Edu-Agent' in page.text
    assert page.headers['cache-control'] == 'no-cache'
    assert '/static/app.js?v=' in page.text and '/static/app.css?v=' in page.text  # content-versioned assets
    js = client.get('/static/app.js')
    assert js.status_code == 200 and js.headers['cache-control'] == 'no-cache'
