"""Every API route must pass through the per-route guard; pages need no token."""

import json

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_open_tasks import FakeOpenModel

from harness.app import create_app
from harness.core.gateway import Gateway


def _dependencies(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependencies(dep)


def test_every_api_route_depends_on_the_guard():
    from harness.interfaces.api import create_router

    router = create_router(lambda: None)
    api_routes = [r for r in router.routes if isinstance(r, APIRoute) and r.path.startswith('/api/')]
    assert len(api_routes) > 30
    unguarded = [
        r.path for r in api_routes if 'guard' not in {getattr(c, '__name__', '') for c in _dependencies(r.dependant)}
    ]
    assert unguarded == []


def test_classroom_mode_serves_pages_but_guards_data(tmp_path, monkeypatch):
    accounts = tmp_path / 'accounts.json'
    accounts.write_text(
        json.dumps(
            {'t': {'role': 'teacher', 'class_id': 'C'}, 's': {'role': 'student', 'class_id': 'C', 'learner': 'A'}}
        )
    )
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE', str(accounts))
    gateway = Gateway(tmp_path / 'db.sqlite', FakeOpenModel())
    with TestClient(create_app(lambda: gateway)) as client:
        for page in ('/', '/student/', '/teacher/', '/static/app.css'):
            assert client.get(page).status_code == 200
        assert client.get('/api/me').status_code == 401
        assert client.get('/api/me', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.get('/api/me', headers={'Authorization': 'Bearer t'}).json()['role'] == 'teacher'
        assert (
            client.get('/api/teacher/assignments?class_id=C', headers={'Authorization': 'Bearer s'}).status_code == 403
        )
        assert (
            client.get('/api/teacher/assignments?class_id=D', headers={'Authorization': 'Bearer t'}).status_code == 403
        )
        assert client.get('/api/teacher/assignments?class_id=C', headers={'Authorization': 'Bearer t'}).json() == []
        # Edits to the accounts file take effect without restarting.
        accounts.write_text(json.dumps({'t2': {'role': 'teacher', 'class_id': 'C'}}))
        import os

        os.utime(accounts, ns=(1, 1))
        assert client.get('/api/me', headers={'Authorization': 'Bearer t'}).status_code == 401
        assert client.get('/api/me', headers={'Authorization': 'Bearer t2'}).status_code == 200
        accounts.write_text('{broken')
        os.utime(accounts, ns=(2, 2))
        assert client.get('/api/me', headers={'Authorization': 'Bearer t2'}).status_code == 503


def test_local_demo_helpers(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_ACCOUNTS_FILE', raising=False)
    gateway = Gateway(tmp_path / 'db.sqlite', FakeOpenModel())
    with TestClient(create_app(lambda: gateway, with_ui=False)) as client:
        assert client.get('/api/me').json() == {'mode': 'local-demo'}
        meta = client.get('/api/meta').json()
        assert 'open' in meta['task_types'] and 'numeric' in meta['criterion_kinds']
        upload = client.post('/api/materials/extract', files={'file': ('notes.md', '月相由日地月位置决定'.encode())})
        assert upload.json()['text'] == '月相由日地月位置决定'
        assert client.post('/api/materials/extract', files={'file': ('x.exe', b'1')}).status_code == 400
        preview = client.post('/api/teacher/algebra/preview', json={'question': '解方程 2x+3=7'}).json()
        assert preview['algebra_kind'] == 'equation'


def test_class_list_comes_from_data_and_is_scoped_in_classroom_mode(tmp_path, monkeypatch):
    gateway = Gateway(tmp_path / 'db.sqlite', FakeOpenModel())
    gateway.open.publish_assignment(
        dict(
            class_id='七年一班',
            course_id='科学',
            concept_id='月相',
            domain='科学',
            objective='解释月相',
            question='月相为何变化',
            rubric='解释原因',
            reference='参考结论',
        )
    )
    monkeypatch.delenv('HARNESS_ACCOUNTS_FILE', raising=False)
    with TestClient(create_app(lambda: gateway, with_ui=False)) as client:
        assert client.get('/api/teacher/classes').json() == ['七年一班']
    accounts = tmp_path / 'accounts.json'
    accounts.write_text(
        json.dumps(
            {
                't': {'role': 'teacher', 'class_id': '八年二班'},
                's': {'role': 'student', 'class_id': '八年二班', 'learner': 'A'},
            }
        )
    )
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE', str(accounts))
    with TestClient(create_app(lambda: gateway, with_ui=False)) as client:
        assert client.get('/api/teacher/classes', headers={'Authorization': 'Bearer t'}).json() == ['八年二班']
        assert client.get('/api/teacher/classes', headers={'Authorization': 'Bearer s'}).status_code == 403
