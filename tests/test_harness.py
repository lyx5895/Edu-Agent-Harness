import base64
import io
import uuid

import pytest
from PIL import Image

from harness.grading.algebra import verify, expression
from harness.legacy.course import ITEMS
from harness.legacy.engine import Engine, Conflict
from harness.llm.model import ModelClient, ModelUnavailable, Diagnosis


class FakeModel:
    base_url='fake'
    def diagnose(self,item,text,verification,resource):
        return {'status':verification['status'],'first_error':verification['first_error'],
            'observation':'检查','hypothesis':'可能遗漏分配','confidence':'high'}
    def teach(self,packet): return '括号外的数应该乘括号里的哪些项？'
    def interpret_reply(self,packet):
        return {'outcome':'unknown','evidence_quote':packet['student_reply'][:160],'hypothesis':'待验证'}
    def hint(self,resource):return '括号外的数需要乘到括号内哪些项呢？'
    def transcribe(self,url):return {'text':'2x+3','uncertain':True,'note':'部分模糊，请确认'}


@pytest.fixture
def engine(tmp_path):return Engine(tmp_path/'test.sqlite',FakeModel())


def op(e,s,action,text='',key=None):
    return e.operation(s['session_id'],key or uuid.uuid4().hex,action,{'text':text} if action in ('attempt','confirm') else {})


def test_course_all_answers_and_disjoint_items():
    assert len([i for i in ITEMS.values() if i['phase']=='teach'])==30
    assert len([i for i in ITEMS.values() if i['phase']=='check'])==20
    for item in ITEMS.values():assert verify(item,item['answer'])['status']=='correct',item
    assert len({i['initial'] for i in ITEMS.values()})==50


def test_first_error_and_completion():
    item=ITEMS['k01-teach-1']
    assert verify(item,'2(x+3)=2x+3\n2x+6')['first_error']==1
    assert verify(item,'2x+6')['status']=='correct'
    assert verify(item,'2(x+3)')['status']=='incomplete'
    assert verify(ITEMS['k07-teach-1'],'2x=10\nx=5')['status']=='correct'
    assert verify(ITEMS['k07-teach-1'],'2x=9\nx=5')['first_error']==1


@pytest.mark.parametrize('text',["__import__('os').system('id')",'x**999999','x/x','9'*200,'sin(x)','[x for x in x]'])
def test_restricted_parser(text):
    with pytest.raises((ValueError,SyntaxError)):expression(text)


def test_hint_practice_check_and_report(engine):
    s=engine.create('A')
    wrong=op(engine,s,'attempt','2x+3')
    assert wrong['status']=='incorrect'
    hint=op(engine,s,'hint')
    assert hint['level']==1
    correct=op(engine,s,'attempt','2x+6')
    assert correct['evidence']=='assisted_complete'
    check=correct['session']['item']
    assert check['phase']=='check'
    assert not {'answer','hints','rubric'} & check.keys()
    with pytest.raises(Conflict):op(engine,s,'hint')
    result=op(engine,s,'attempt',ITEMS[check['id']]['answer'])
    assert result['evidence']=='independent_check_passed'
    assert engine.progress('A')['summary'][0]['independent_check_passed']
    report=engine.report('A')
    assert report['attempts'][0]['text']=='2x+3'


def test_idempotency_collision_and_restart(engine):
    s=engine.create('A');sid=s['session_id']
    a=op(engine,s,'attempt','2x+3','same')
    b=op(engine,s,'attempt','2x+3','same')
    assert a==b and len(engine.report()['attempts'])==1
    with pytest.raises(Conflict):op(engine,s,'attempt','2x+6','same')
    restarted=Engine(engine.db,FakeModel())
    assert restarted.session(sid)['history']==engine.session(sid)['history']
    with pytest.raises(Conflict):engine.operation(sid,'stale','attempt',{'text':'2x+6'},version=0)


def test_failure_no_learning_or_idempotency_write(engine):
    s=engine.create('A')
    def fail(*args):raise ModelUnavailable('timeout')
    engine.model.diagnose=fail
    with pytest.raises(ModelUnavailable):op(engine,s,'attempt','2x+3','retry')
    assert engine.report()['attempts']==[]
    assert engine.session(s['session_id'])['version']==0
    engine.model=FakeModel()
    assert op(engine,s,'attempt','2x+3','retry')['status']=='incorrect'


def test_two_failures_escalate_and_check_returns_to_teaching(engine):
    s=engine.create('A')
    op(engine,s,'attempt','2x+3')
    r=op(engine,s,'attempt','2x+3')
    assert r['session']['hint_level']==1
    r=op(engine,s,'attempt','2x+6')
    checkid=r['session']['item']['id']
    r=op(engine,s,'attempt','x')
    assert r['session']['item']['phase']=='teach'
    r=op(engine,s,'attempt',ITEMS[r['session']['item']['id']]['answer'])
    assert r['session']['item']['phase']=='check' and r['session']['item']['id']!=checkid


def test_teacher_correction_revises_progress_preserves_evidence(engine):
    s=engine.create('A');r=op(engine,s,'attempt','2x+6')
    r=op(engine,s,'attempt',ITEMS[r['session']['item']['id']]['answer'])
    engine.correct(r['event_id'],'incorrect','教师发现抄写错误','fix')
    report=engine.report()
    assert report['attempts'][-1]['original_status']=='correct'
    assert report['attempts'][-1]['status']=='incorrect'
    assert report['attempts'][-1]['original_evidence']=='independent_check_passed'
    assert report['attempts'][-1]['evidence']=='attempted'
    assert not report['summary'][0]['independent_check_passed']
    assert engine.session(s['session_id'])['needs_review']
    assert len(report['revisions'])==1
    engine.correct(r['event_id'],'incorrect','教师发现抄写错误','fix')
    assert len(engine.report()['revisions'])==1


def test_image_requires_confirmation(engine):
    buf=io.BytesIO();Image.new('RGB',(64,64),'white').save(buf,format='PNG')
    url='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
    s=engine.create('A')
    r=engine.operation(s['session_id'],'img','image',{'image_url':url})
    assert r['transcription']['uncertain']
    assert not engine.report()['attempts']
    with pytest.raises(Conflict):op(engine,s,'attempt','2x+6')
    result=op(engine,s,'confirm','2x+6')
    assert result['status']=='correct'
    assert engine.report()['attempts'][0]['confirmed']
    assert engine.report()['attempts'][0]['image_event_id']
    assert engine.session(s['session_id'])['pending_image'] is None


def test_cross_session_check_not_reused(engine):
    s1=engine.create('A');s2=engine.create('A')
    c1=op(engine,s1,'attempt','2x+6')['session']['item']['id']
    c2=op(engine,s2,'attempt','2x+6')['session']['item']['id']
    assert c1!=c2
    s3=engine.create('A')
    assert op(engine,s3,'attempt','2x+6')['session']['done']


def test_json_repair_once_and_unknown_not_mastery():
    m=ModelClient();calls=[]
    def invalid(*args,**kwargs):calls.append(1);return 'not json'
    m.chat=invalid
    with pytest.raises(ModelUnavailable):m.structured(Diagnosis,'test','test')
    assert len(calls)==2


def test_uncertain_text_does_not_claim_mastery(engine):
    s=engine.create('A')
    engine.model.diagnose=lambda *a:{'status':'correct','first_error':None,'observation':'不确定','hypothesis':'','confidence':'low'}
    r=op(engine,s,'attempt','我觉得自己已经学会了')
    assert r['status']=='unknown'
    assert not engine.progress('A')['summary'][0]['independent_check_passed']


def test_hint_does_not_leak_model_equation(engine):
    s=engine.create('A')
    engine.model.teach=lambda _: '答案是2x+6'
    r=op(engine,s,'hint')
    assert '2x+6' not in r['hint']
