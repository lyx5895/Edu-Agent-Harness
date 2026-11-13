import json
import pytest
from fastapi.testclient import TestClient
from harness.engine import Engine
from harness.model import ModelUnavailable
from test_harness import FakeModel


@pytest.fixture
def client(tmp_path,monkeypatch):
    import harness.app as module
    monkeypatch.setattr(module,'engine',Engine(tmp_path/'api.sqlite',FakeModel()))
    with TestClient(module.app) as c:yield c,module.engine


def test_page_and_api_flow(client):
    c,e=client
    assert c.get('/').status_code==200
    assert c.get('/health').json()['ready']
    assert len(c.get('/api/skills').json())==10
    s=c.post('/api/sessions',json={'learner':'api-test','skill':'k01'}).json()
    path=f"/api/sessions/{s['session_id']}"
    r=c.post(path+'/attempt',json={'request_id':'one','text':'2x+3','version':0})
    assert r.status_code==200 and r.json()['status']=='incorrect'
    assert c.post(path+'/attempt',json={'request_id':'one','text':'2x+3','version':0}).json()==r.json()
    assert c.post(path+'/attempt',json={'request_id':'one','text':'2x+6','version':0}).status_code==409
    for level in (1,2,3):
        r=c.post(path+'/hint',json={'request_id':str(level)}).json()
        assert r['level']==level
    assert '2x+6' in r['hint']
    r=c.post(path+'/attempt',json={'request_id':'correct','text':'2x+6'}).json()
    assert r['evidence']=='assisted_complete'
    assert '5x+30' not in json.dumps(r,ensure_ascii=False)
    assert c.post(path+'/hint',json={'request_id':'check-hint'}).status_code==409
    assert c.get(path).json()['item']['phase']=='check'
    assert c.get('/api/teacher/report').json()['attempts'][0]['text']=='2x+3'


def test_api_timeout_and_invalid_image_leave_state(client):
    c,e=client
    s=c.post('/api/sessions',json={'learner':'api-failure','skill':'k01'}).json()
    path=f"/api/sessions/{s['session_id']}"
    assert c.post(path+'/image',json={'request_id':'bad','image_url':'http://localhost/secret'}).status_code==400
    def fail(*args):raise ModelUnavailable('timeout')
    e.model.diagnose=fail
    assert c.post(path+'/attempt',json={'request_id':'fail','text':'2x+3'}).status_code==503
    assert c.get(path).json()['version']==0
    assert c.get('/api/teacher/report').json()['attempts']==[]


def test_reply_trace_and_replay_api(client):
    c,e=client
    s=c.post('/api/sessions',json={'learner':'dialogue-api','skill':'k01'}).json()
    path=f"/api/sessions/{s['session_id']}"
    r=c.post(path+'/attempt',json={'request_id':'a','text':'2x+3'}).json()
    assert r['teaching_action']=='probe'
    reply=c.post(path+'/reply',json={'request_id':'b','text':'我不理解'}).json()
    assert reply['evidence']=='dialogue_only'
    assert reply['teaching_action']=='clarify'
    trace=c.get(f"/api/teacher/sessions/{s['session_id']}/trace").json()
    assert len(trace)==2 and trace[0]['context']['student_context']['verification']['first_error']==1
    replay=c.get(f"/api/teacher/sessions/{s['session_id']}/replay").json()
    assert replay['passed'] and not replay['model_called']
    assert c.get('/api/teacher/sessions/missing/trace').status_code==400
