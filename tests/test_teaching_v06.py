import json
import pytest
from fastapi.testclient import TestClient
from harness.open_engine import OpenEngine
from harness.gateway import Gateway
from harness.model import ModelUnavailable
from harness.tasks import TaskDraft
from harness.criteria import tool_grade, register_grader, GRADERS
from harness.pedagogy import bind_check_contract, register_profile, profile, PROFILES
from harness.errors import Conflict
from test_open_tasks import FakeOpenModel, act


def assignment(**extra):
    return dict(class_id='C',course_id='语文',concept_id='证据',domain='语文',objective='根据原文解释判断',
                question='解释材料中的观点',rubric='提供原文依据并解释',reference='正确解释',**extra)


class ContractModel(FakeOpenModel):
    def assess_criterion(self,task,criterion,text,context):
        return dict(status='correct',confidence='high',evidence_quote=text,observation='符合本项要求',hypothesis='')

    def plan(self,request,sources,mode,previous):
        result=super().plan(request,sources,mode,previous)
        if previous:
            result['criteria']=[dict(x,expected='') for x in previous['contract']['criteria']]
        return result


@pytest.fixture
def engine(tmp_path):return OpenEngine(tmp_path/'test.sqlite',ContractModel())


def test_contract_missing_dimension_is_rejected(engine):
    aid=engine.publish_assignment(assignment(criteria=[dict(id='reason',kind='model',description='解释依据')]))['assignment_id']
    s=engine.create('A',assignment_id=aid);act(engine,s,'attempt','正确解释')
    original=engine.model.plan
    engine.model.plan=lambda *a:dict(original(*a),criteria=[])
    before=engine.session(s['session_id'])
    with pytest.raises(ModelUnavailable):act(engine,s,'check')
    assert engine.session(s['session_id'])==before


def test_check_preserves_requirements_and_regenerates_expected():
    original=assignment(task_type='experiment',check_requirements=['检查单位'],
        criteria=[dict(id='value',kind='numeric',description='数值正确',expected='4')])
    candidate=dict(question='求新数值',rubric='数值正确',reference='8',criteria=[dict(id='value',kind='numeric',description='新描述',expected='8')])
    new=bind_check_contract(candidate,original)
    assert new['criteria'][0]['expected']=='8'
    assert new['criteria'][0]['description']=='数值正确'
    assert new['check_requirements']==['检查单位']
    assert new['check_contract']['original_rubric']==original['rubric']


def test_generated_check_carries_contract(engine):
    aid=engine.publish_assignment(assignment(task_type='reading',check_requirements=['解释证据与结论的关系'],
        criteria=[dict(id='reason',kind='model',description='解释依据')]))['assignment_id']
    s=engine.create('A',assignment_id=aid);act(engine,s,'attempt','正确解释');r=act(engine,s,'check')
    assert r['session']['task']['task_type']=='reading'
    assert r['session']['task']['criteria'][0]['id']=='reason'
    assert r['session']['task']['check_requirements']==['解释证据与结论的关系']
    assert 'original_rubric' not in json.dumps(r)


def test_dimension_success_cannot_erase_whole_task_failure(engine):
    aid=engine.publish_assignment(assignment(criteria=[dict(id='one',kind='model',description='只检查一项')]))['assignment_id']
    s=engine.create('A',assignment_id=aid)
    r=act(engine,s,'attempt','错误解释')
    assert r['status']=='incorrect' and not r['session']['completed']
    with engine.connect() as c:
        p=json.loads(c.execute('SELECT payload FROM learning_events WHERE id=?',(r['event_id'],)).fetchone()[0])
    assert p['rubric_conflict'] and p['dimension_status']=='correct'
    assert engine.dashboard('C')['review_queue'][0]['priority']=='urgent'


def test_scoped_plugins_and_registration():
    criterion=dict(id='quote',kind='source_quote',description='引用原文',expected='蒸发吸热')
    assert tool_grade(criterion,{'material':'蒸发吸热。'},'原文说蒸发吸热')['status']=='correct'
    assert tool_grade(criterion,{'material':'其他内容'},'蒸发吸热')['status']=='unknown'
    assert tool_grade(dict(criterion,kind='word_count',expected='3:4'),{},'one two three')['status']=='correct'
    register_grader('test_custom',lambda *a:dict(status='unknown',reason='无法验证'))
    register_profile('test_custom','测试','请求补充实验条件')
    try:
        assert tool_grade(dict(criterion,kind='test_custom'),{},'abc')['status']=='unknown'
        assert profile({'task_type':'test_custom'})['label']=='测试'
        assert profile({'task_type':'new_subject'})==PROFILES['open']
    finally:
        GRADERS.pop('test_custom');PROFILES.pop('test_custom')


def test_unknown_plugin_and_invalid_source_rejected(engine):
    for criterion in [dict(id='x',kind='missing_plugin',description='检查'),
                      dict(id='x',kind='source_quote',description='引文',expected='不存在'),
                      dict(id='x',kind='word_count',description='长度',expected='9:2')]:
        with pytest.raises(ValueError):engine.publish_assignment(assignment(criteria=[criterion]))


def test_teacher_bank_used_without_generation_and_exhaustion(engine):
    check=engine.publish_assignment(assignment(task_type='reading',check_requirements=['说明依据']))['assignment_id']
    data=assignment(task_type='reading',check_requirements=['说明依据'],check_assignment_ids=[check])
    data['question']='另一段文字的观点'
    aid=engine.publish_assignment(data)['assignment_id']
    s=engine.create('A',assignment_id=aid);act(engine,s,'attempt','正确解释')
    r=act(engine,s,'check',key='bank-check')
    assert act(engine,s,'check',key='bank-check')==r
    assert engine.model.plans==0
    assert r['session']['task']['question']=='解释材料中的观点'
    act(engine,s,'attempt','正确解释');act(engine,s,'resume_practice');act(engine,s,'attempt','正确解释')
    with pytest.raises(ModelUnavailable):act(engine,s,'check')


def test_bank_cannot_cross_class_or_drop_requirement(engine):
    data=assignment();data['class_id']='D'
    other=engine.publish_assignment(data)['assignment_id']
    with pytest.raises(ValueError):engine.publish_assignment(assignment(check_assignment_ids=[other]))
    check=engine.publish_assignment(assignment())['assignment_id']
    with pytest.raises(ValueError):engine.publish_assignment(assignment(check_assignment_ids=[check],check_requirements=['新的必要要求']))


def test_pack_edit_publish_restart_and_inbox(engine):
    second=assignment(task_type='experiment');second.update(course_id='物理',concept_id='受力',domain='物理')
    data=dict(class_id='C',title='跨课程备课',items=[assignment(task_type='reading'),second])
    p=engine.save_pack(data,'save');assert engine.save_pack(data,'save')==p
    changed={**data,'title':'修改后的备课'}
    p=engine.save_pack(changed,'edit',p['pack_id'],p['version'])
    with pytest.raises(Conflict):engine.save_pack(data,'stale',p['pack_id'],1)
    distribution=dict(learners=['A','B'],reviewed=True,review_note='逐题核对')
    result=engine.publish_pack(p['pack_id'],p['version'],distribution,'publish')
    assert engine.publish_pack(p['pack_id'],p['version'],distribution,'publish')==result
    assert result['result']['distributed_tasks']==4
    restored=OpenEngine(engine.db,ContractModel())
    inbox=restored.inbox('C','A');assert len(inbox)==2
    assert {i['course_id'] for i in inbox}=={'语文','物理'}
    assert 'reference' not in json.dumps(inbox) and 'rubric' not in json.dumps(inbox)
    assert restored.create('A',followup_id=inbox[0]['followup_id'])['task']
    with pytest.raises(Conflict):restored.save_pack(data,'after',p['pack_id'],result['version'])


def test_atomic_pack_rejects_invalid_second_item(engine):
    bad=assignment(criteria=[dict(id='bad',kind='source_quote',description='引用',expected='不存在')])
    p=engine.save_pack(dict(class_id='C',title='需核对',items=[assignment(),bad]),'save')
    with pytest.raises(ValueError):engine.publish_pack(p['pack_id'],1,dict(learners=['A'],reviewed=True,review_note='审核'),'pub')
    with engine.connect() as c:assert c.execute('SELECT count(*) FROM teaching_assignments').fetchone()[0]==0
    assert engine.followups('C')==[] and engine.load_pack(p['pack_id'])['status']=='draft'


def test_pack_generation_is_draft_until_review(engine):
    p=engine.generate_pack(dict(class_id='C',title='多学科草稿',goals=[dict(course_id='英语',concept_id='essay',domain='英语',
        objective='写一段论证',task_type='writing')]),'generate')
    assert p['status']=='draft' and p['body']['items'][0]['task_type']=='writing'
    assert engine.inbox('C','A')==[]
    with pytest.raises(ValueError):engine.publish_pack(p['pack_id'],1,dict(learners=['A'],reviewed=False,review_note='未审核'),'p')


def test_pack_api_class_and_student_isolation(tmp_path,monkeypatch):
    import harness.app as app
    g=Gateway(tmp_path/'api.sqlite',ContractModel());monkeypatch.setattr(app,'engine',g)
    config=tmp_path/'accounts.json'
    config.write_text(json.dumps({'t':{'role':'teacher','class_id':'C'},'o':{'role':'teacher','class_id':'D'},
        'a':{'role':'student','class_id':'C','learner':'A'},'b':{'role':'student','class_id':'C','learner':'B'}}))
    monkeypatch.setenv('HARNESS_ACCOUNTS_FILE',str(config));h=lambda token:dict(Authorization='Bearer '+token)
    with TestClient(app.app) as client:
        body=dict(class_id='C',title='草稿',items=[assignment()],request_id='save')
        assert client.post('/api/teacher/packs',json=body,headers=h('a')).status_code==403
        p=client.post('/api/teacher/packs',json=body,headers=h('t')).json()
        url='/api/teacher/packs/'+p['pack_id']
        assert client.get(url,headers=h('o')).status_code==403
        assert client.get(url,headers=h('a')).status_code==403
        assert client.get('/api/teacher/packs?class_id=C',headers=h('t')).status_code==200
        publish=dict(version=1,learners=['A'],reviewed=True,review_note='已审核',request_id='pub')
        assert client.post(url+'/publish',json=publish,headers=h('t')).status_code==200
        assert client.get('/api/learning/inbox?class_id=C&learner=A',headers=h('b')).status_code==403
        inbox=client.get('/api/learning/inbox?class_id=C&learner=A',headers=h('a')).json()
        assert len(inbox)==1
        assert client.post('/api/sessions',json=dict(learner='A',followup_id=inbox[0]['followup_id']),headers=h('a')).status_code==200


def test_structured_arrays_and_criterion_images():
    from harness.open_model import OpenModel
    model=OpenModel();seen=[]
    draft=dict(domain='语文',objective='理解文本',question='说明理由',rubric='说明',reference='理由')
    model.chat=lambda messages,**kw:(seen.append(messages) or json.dumps(draft))
    assert model.structured(TaskDraft,'生成','材料').criteria==[]
    assert 'criteria必须是JSON数组' in seen[0][0]['content']
    capture=[]
    def structured(schema,instruction,payload):
        capture.append(payload)
        return schema(status='correct',confidence='high',evidence_quote='原文',observation='判断',hypothesis='')
    model.structured=structured
    model.assess_criterion(draft,{'description':'图像证据'},'原文',{'image_url':'data:image/png;base64,AA=='})
    assert capture[0][1]['type']=='image_url'


def test_quote_only_does_not_count_as_additional_explanation(engine):
    data=assignment(task_type='reading',material='雨水流向低处。',criteria=[dict(id='quote',kind='source_quote',description='原文',expected='雨水流向低处。')])
    data['rubric']='引用原文并解释这与观察结果的关系。'
    original=engine.model.assess
    engine.model.assess=lambda *a:dict(original(*a),status='correct')
    aid=engine.publish_assignment(data)['assignment_id'];s=engine.create('A',assignment_id=aid)
    assert act(engine,s,'attempt','“雨水流向低处。”')['status']=='incomplete'
    assert act(engine,s,'attempt','雨水流向低处。因为这里地势低，所以会积水。')['status']=='correct'
    # A pure quotation task does not require additional prose.
    data['rubric']='引用指定句子';data['question']='摘录指定句子'
    aid=engine.publish_assignment(data)['assignment_id'];s=engine.create('B',assignment_id=aid)
    assert act(engine,s,'attempt','雨水流向低处。')['status']=='correct'


def test_edit_publish_failure_does_not_advance_draft(engine):
    body=dict(class_id='C',title='原草稿',items=[assignment()])
    p=engine.save_pack(body,'save')
    changed={**body,'title':'新草稿','items':[assignment(criteria=[dict(id='bad',kind='source_quote',description='引用',expected='缺少来源')])]}
    with pytest.raises(ValueError):
        engine.publish_pack(p['pack_id'],1,dict(reviewed=True,review_note='检查',learners=['A']),'pub',edited_body=changed)
    assert engine.load_pack(p['pack_id'])==p


def test_ui_edits_publish_visible_content_and_resets_approval(engine,monkeypatch):
    import harness.app as app
    from types import SimpleNamespace
    monkeypatch.setattr(app,'engine',SimpleNamespace(open=engine))
    p=engine.save_pack(dict(class_id='C',title='备课',items=[assignment()]),'s')
    displayed=app.pack_display(p);rows=displayed[2]
    rows[0][5]='修改后的题目'
    output=app.publish_pack_ui(p,rows,[],'A',True,'已核对')
    assert output[-1] is False
    assert output[0]['status']=='published'
    s=engine.create('A',followup_id=engine.inbox('C','A')[0]['followup_id'])
    assert s['task']['question']=='修改后的题目'
