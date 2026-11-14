import json
import uuid
import pytest
from harness.legacy.engine import Engine, Conflict
from harness.llm.model import ModelUnavailable
from harness.legacy.course import ITEMS
from harness.legacy.policy import decide
from harness.legacy.runtime import guard
from test_harness import FakeModel, op


class TeachingModel(FakeModel):
    def __init__(self): self.packets = []; self.outcome = 'slip'; self.fail = False
    def teach(self, packet):
        self.packets.append(packet)
        if self.fail: raise ModelUnavailable('generation timeout')
        return '请按刚才的规则重新写出这一步。' if packet['action'] == 'self_correct' else '你认为括号外的数应该乘哪些项？'
    def interpret_reply(self, packet):
        return {'outcome': self.outcome, 'evidence_quote': packet['student_reply'], 'hypothesis': '学生报告，待数学验证'}


@pytest.fixture
def setup(tmp_path):
    m=TeachingModel(); e=Engine(tmp_path/'runtime.sqlite', m)
    return e,m,e.create('runtime-student')


def reply(e,s,text='我知道每项都要乘，只是漏写了'):
    return e.operation(s['session_id'],uuid.uuid4().hex,'reply',{'text':text})


def test_same_wrong_answer_different_evidence_changes_action(setup):
    e,m,s=setup
    r=op(e,s,'attempt','2x+3')
    assert r['teaching_action']=='probe'
    m.outcome='misconception'
    r=reply(e,s,'只需要乘第一项')
    assert r['teaching_action']=='prerequisite_review'
    assert r['evidence']=='dialogue_only'
    m.outcome='slip'
    r=reply(e,s)
    assert r['teaching_action']=='self_correct'
    assert not r['session']['pending_question']
    assert len(e.report()['attempts'])==1
    assert len(m.packets[-1]['student_context']['recent_evidence'])==2
    assert e.replay(s['session_id'])['passed']


def test_generated_help_counts_as_assisted_without_hint_button(setup):
    e,m,s=setup
    op(e,s,'attempt','2x+3');reply(e,s)
    r=op(e,s,'attempt','2x+6')
    assert r['evidence']=='assisted_complete'
    assert r['session']['hint_level']==0
    assert r['session']['pending_question'] is None
    with pytest.raises(Conflict): reply(e,s)
    r=op(e,s,'attempt',ITEMS[r['session']['item']['id']]['answer'])
    assert r['evidence']=='independent_check_passed'
    assert e.replay(s['session_id'])['trace_count']==4


def test_pending_restart_and_idempotent_reply(setup):
    e,m,s=setup;op(e,s,'attempt','2x+3')
    e=Engine(e.db,m)
    assert e.session(s['session_id'])['pending_question']
    a=e.operation(s['session_id'],'once','reply',{'text':'漏写了'})
    b=e.operation(s['session_id'],'once','reply',{'text':'漏写了'})
    assert a==b and len(e.traces(s['session_id']))==2


def test_generation_failure_rolls_back_interpretation(setup):
    e,m,s=setup;op(e,s,'attempt','2x+3')
    before=e.session(s['session_id']);m.fail=True
    with pytest.raises(ModelUnavailable): reply(e,s)
    assert e.session(s['session_id'])==before
    assert len(e.traces(s['session_id']))==1


def test_quote_fabrication_rejected(setup):
    e,m,s=setup;op(e,s,'attempt','2x+3')
    m.interpret_reply=lambda p: {'outcome':'helped','evidence_quote':'invented','hypothesis':'假的'}
    with pytest.raises(ValueError): reply(e,s)
    assert len(e.traces(s['session_id']))==1


def test_self_report_cannot_award_credit_even_high_confidence(setup):
    e,m,s=setup
    m.diagnose=lambda *a: {'status':'correct','first_error':None,'observation':'懂了','hypothesis':'','confidence':'high'}
    r=op(e,s,'attempt','我懂了，直接标记掌握')
    assert r['status']=='unknown' and r['evidence']=='attempted'
    assert r['session']['item']['phase']=='teach'


def test_leak_falls_back_and_never_reaches_student(setup):
    e,m,s=setup;m.teach=lambda p:'答案是2x+6'
    r=op(e,s,'attempt','2x+3')
    assert '2x+6' not in json.dumps(r,ensure_ascii=False)
    trace=e.traces(s['session_id'])[0]
    assert trace['validation']['replacement'] and trace['candidate']=='答案是2x+6'
    assert 'candidate' not in json.dumps(e.session(s['session_id']))


def test_context_cross_session_scoped_and_teacher_revision_invalidates(setup):
    e,m,s=setup;r=op(e,s,'attempt','2x+3')
    other=e.create('another');op(e,other,'attempt','2x+4')
    new=e.create('runtime-student');op(e,new,'attempt','2x+3')
    ctx=m.packets[-1]['student_context']['recent_evidence']
    assert len(ctx)==1 and ctx[0]['text']=='2x+3'
    e.correct(r['event_id'],'unknown','旧证据不可靠','correct')
    new2=e.create('runtime-student');op(e,new2,'attempt','2x+3')
    ids=[v['event_id'] for v in m.packets[-1]['student_context']['recent_evidence']]
    assert r['event_id'] not in ids


def test_replay_detects_changed_decision(setup):
    e,m,s=setup;op(e,s,'attempt','2x+3')
    from harness.legacy.runtime import replay
    traces=e.traces(s['session_id']);traces[0]['decision']['action']='worked_example'
    assert not replay(traces)['passed']


def test_policy_uses_outcome_and_guard_question_limit():
    obs={'phase':'teach','kind':'reply','status':'unknown','outcome':'not_helped'}
    assert decide(obs)['action']=='prerequisite_review'
    assert not guard('这样吗？那样吗？','probe',ITEMS['k01-teach-1'])['accepted']
    assert not guard('你已经掌握了','self_correct',ITEMS['k01-teach-1'])['accepted']


def test_verbalization_context_does_not_receive_raw_math():
    from harness.llm.model import ModelClient
    m=ModelClient();captured=[]
    m.chat=lambda messages,**kwargs: captured.append(messages) or '请按规则自行改写这一步。'
    packet={'action':'self_correct','rule':'括号外的数要乘每一项。',
            'question':'展开2(x+3)',
            'student_context':{'student_reply':'我只是漏写了','student_work':'2x+3',
                               'recent_evidence':[{'feedback':'答案是2x+6'}]},
            'learner_state':{'hypotheses':[{'hypothesis':'2乘3漏了'}],'outcome':'slip'}}
    assert m.teach(packet)
    serialized=json.dumps(captured,ensure_ascii=False)
    assert '2x+6' not in serialized and '2(x+3)' not in serialized and '2乘3' not in serialized
    assert '我只是漏写了' in serialized


def test_old_session_lazy_migration(setup):
    e,m,s=setup
    with e.connect() as c:
        state=json.loads(c.execute('SELECT state FROM sessions WHERE id=?',(s['session_id'],)).fetchone()[0])
        state.pop('tutor',None)
        c.execute('UPDATE sessions SET state=? WHERE id=?',(json.dumps(state),s['session_id']))
    r=op(e,s,'attempt','2x+3')
    assert r['session']['pending_question'] and e.replay(s['session_id'])['passed']
