"""Teacher-owned curriculum DAGs and durable review/remediation work.
No automatic claims of mastery. Paths advance on valid independent observations.
"""
import json
import time
import uuid
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from harness.teaching import teacher
from harness.errors import Conflict
from harness.core.memory import summarize


class PathNode(BaseModel):
    concept_id: str = Field(min_length=1,max_length=80)
    title: str = Field(min_length=1,max_length=160)
    assignment_id: str
    prerequisite_ids: list[str] = Field(default_factory=list,max_length=20)


class Curriculum(BaseModel):
    class_id: str = Field(min_length=1,max_length=80)
    course_id: str = Field(min_length=1,max_length=80)
    title: str = Field(min_length=1,max_length=160)
    nodes: list[PathNode] = Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def dag(self):
        graph={n.concept_id:n.prerequisite_ids for n in self.nodes}
        if len(graph)!=len(self.nodes): raise ValueError('课程知识点不能重复')
        visiting=set();done=set()
        def visit(key):
            if key not in graph: raise ValueError('先修知识点不在课程路径内')
            if key in visiting: raise ValueError('课程先修关系不能成环')
            if key in done: return
            visiting.add(key)
            for parent in graph[key]: visit(parent)
            visiting.remove(key);done.add(key)
        for key in graph:visit(key)
        return self


class FollowupBatch(BaseModel):
    class_id: str = Field(min_length=1,max_length=80)
    assignment_id: str
    learners: list[str] = Field(min_length=1,max_length=100)
    kind: Literal['review','remediation','diagnostic'] = 'remediation'
    due_at: float = Field(ge=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=400)

    @model_validator(mode='after')
    def learners_valid(self):
        if any(not x.strip() or len(x)>80 for x in self.learners): raise ValueError('学生名称无效')
        if len(set(self.learners))!=len(self.learners):raise ValueError('学生名单不能重复')
        return self


def initialize(c):
    c.execute('CREATE TABLE IF NOT EXISTS teaching_paths(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS teaching_followups(id TEXT PRIMARY KEY,learner TEXT,class_id TEXT,body TEXT,status TEXT,due_at REAL,session_id TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS followup_events(id INTEGER PRIMARY KEY AUTOINCREMENT,followup_id TEXT,kind TEXT,payload TEXT,at REAL)')
    c.execute('CREATE INDEX IF NOT EXISTS followup_scope ON teaching_followups(class_id,learner,status,due_at)')


def publish_path(c,data):
    body=Curriculum.model_validate(data).model_dump()
    for node in body['nodes']:
        a=teacher.load(c,node['assignment_id'])
        if any(a[k]!=body[k] for k in ('class_id','course_id')) or a['concept_id']!=node['concept_id']:
            raise ValueError('路径任务必须属于同一班级、课程和指定知识点')
    key=uuid.uuid4().hex
    c.execute('INSERT INTO teaching_paths VALUES(?,?)',(key,json.dumps(body,ensure_ascii=False)))
    return {'path_id':key,**body}


def load_path(c,key):
    row=c.execute('SELECT body FROM teaching_paths WHERE id=?',(key,)).fetchone()
    if not row: raise ValueError('课程路径不存在')
    return json.loads(row['body'])


def event(c,key,kind,payload):
    c.execute('INSERT INTO followup_events(followup_id,kind,payload,at) VALUES(?,?,?,?)',
              (key,kind,json.dumps(payload,ensure_ascii=False),time.time()))


def load_followup(c,key):
    row=c.execute('SELECT * FROM teaching_followups WHERE id=?',(key,)).fetchone()
    if not row: raise ValueError('跟进任务不存在')
    return {**dict(row),'body':json.loads(row['body'])}


def assign(c,data):
    body=FollowupBatch.model_validate(data).model_dump()
    a=teacher.load(c,body['assignment_id'])
    if a['class_id']!=body['class_id']:raise ValueError('不能跨班级布置任务')
    result=[]
    for learner in body['learners']:
        # Review/diagnostic evidence requires a genuinely unexposed prompt.
        if body['kind'] in ('review','diagnostic'):
            for row in c.execute('SELECT state FROM learning_sessions WHERE learner=?',(learner,)):
                s=json.loads(row['state'])
                if s.get('scope',{}).get('class_id')==a['class_id'] and a['question'].strip() in [q.strip() for q in s.get('seen',[])]:
                    raise ValueError('复习/诊断请使用该学生尚未见过的新题：'+learner)
        duplicate=c.execute("SELECT id FROM teaching_followups WHERE learner=? AND class_id=? AND status IN ('queued','active') AND json_extract(body,'$.assignment_id')=?",
                            (learner,body['class_id'],body['assignment_id'])).fetchone()
        if duplicate:result.append(load_followup(c,duplicate['id']));continue
        key=uuid.uuid4().hex
        item={k:v for k,v in body.items() if k!='learners'}
        item.update(course_id=a['course_id'],concept_id=a['concept_id'],objective=a['objective'])
        c.execute('INSERT INTO teaching_followups VALUES(?,?,?,?,?,?,?)',
                  (key,learner,body['class_id'],json.dumps(item,ensure_ascii=False),'queued',body['due_at'],None))
        event(c,key,'assigned',item)
        result.append(load_followup(c,key))
    return result


def list_followups(c,class_id=None,learner=None):
    rows=c.execute('SELECT id FROM teaching_followups WHERE (? IS NULL OR class_id=?) AND (? IS NULL OR learner=?) ORDER BY due_at,id',
                   (class_id,class_id,learner,learner)).fetchall()
    now=time.time()
    result=[]
    for row in rows:
        item=load_followup(c,row['id']);item['due']=item['due_at']<=now
        last=c.execute('SELECT kind,payload,at FROM followup_events WHERE followup_id=? ORDER BY id DESC LIMIT 1',(row['id'],)).fetchone()
        item['last_event']={'kind':last['kind'],'at':last['at'],'details':json.loads(last['payload'])} if last else None
        result.append(item)
    return result


def revise(c,key,action,due_at,reason):
    f=load_followup(c,key)
    if f['status'] not in ('queued','active'):raise Conflict('已结束的跟进任务不能再修改')
    if action=='postpone':
        if f['status']!='queued' or due_at is None or due_at<time.time():raise ValueError('仅待开始任务可延期到未来时间')
        c.execute('UPDATE teaching_followups SET due_at=? WHERE id=?',(due_at,key))
    elif action=='cancel':
        c.execute("UPDATE teaching_followups SET status='cancelled' WHERE id=?",(key,))
    else:raise ValueError('不支持的跟进操作')
    event(c,key,action,{'due_at':due_at,'reason':reason})
    return load_followup(c,key)


def observe(c,sid,payload,event_id):
    f=c.execute("SELECT id,body,status FROM teaching_followups WHERE session_id=?",(sid,)).fetchone()
    if not f or f['status']!='active' or not payload.get('status'):return
    # Completing guided remediation still requires a fresh independent check.
    independent=payload.get('evidence') in ('independent_verified','independent_model_assessed')
    if independent:
        c.execute("UPDATE teaching_followups SET status='completed' WHERE id=?",(f['id'],))
        event(c,f['id'],'completed',{'event_id':event_id,'evidence':payload['evidence']})
    elif payload['status'] in ('incorrect','incomplete','unknown') and payload.get('task',{}).get('phase')=='check':
        event(c,f['id'],'check_not_passed',{'event_id':event_id,'status':payload['status']})


def invalidate(c,sids):
    for sid in sids:
        for row in c.execute("SELECT id FROM teaching_followups WHERE session_id=? AND status IN ('active','completed')",(sid,)):
            c.execute("UPDATE teaching_followups SET status='needs_recheck' WHERE id=?",(row['id'],))
            event(c,row['id'],'evidence_invalidated',{'session_id':sid})


def next_step(c,path_id,learner,now=None):
    now=time.time() if now is None else now
    path=load_path(c,path_id)
    memories=summarize(c,learner,{'class_id':path['class_id'],'course_id':path['course_id']},now=now)
    memory={m['scope'].get('concept_id'):m for m in memories}
    nodes={n['concept_id']:n for n in path['nodes']}
    observed={k for k,m in memory.items() if m.get('independent_current') and m['state'] not in ('needs_support','needs_recheck')}
    base={'path_id':path_id,'learner':learner,'nodes':[{**n,'state':memory.get(n['concept_id'],{}).get('state','insufficient_evidence')} for n in path['nodes']]}
    pending=[]
    for row in c.execute('SELECT id,state FROM learning_sessions WHERE learner=? ORDER BY rowid DESC',(learner,)):
        s=json.loads(row['state']);scope=s.get('scope',{})
        if scope.get('class_id')!=path['class_id'] or scope.get('course_id')!=path['course_id'] or scope.get('concept_id') not in nodes:continue
        if s.get('followup_id') and load_followup(c,s['followup_id'])['status'] in ('cancelled','needs_recheck'):continue
        if not s['completed'] and not s['needs_review']:
            pending.append(row['id'])
    due=[f for f in list_followups(c,path['class_id'],learner) if f['status']=='queued' and f['due_at']<=now and f['body']['course_id']==path['course_id'] and f['body']['concept_id'] in nodes]
    if due:return {**base,'action':'followup','followup_id':due[0]['id'],'reason':'先完成教师布置的到期复习或补救任务。'}
    if pending:return {**base,'action':'resume','session_id':pending[0],'reason':'先处理尚未完成的作答；旧会话可由教师安排替代任务。'}
    overdue=[m for m in memories if m['review_due'] and m['scope'].get('concept_id') in nodes]
    if overdue:return {**base,'action':'request_review','concept_id':overdue[0]['scope']['concept_id'],'reason':'已有独立证据到期，请教师选择未见复习题。'}
    # Topological readiness, irrespective of the order in which nodes were authored.
    for node in path['nodes']:
        key=node['concept_id']
        if key in observed:continue
        if not set(node['prerequisite_ids'])<=observed:continue
        state=memory.get(key,{}).get('state')
        action='check' if state in ('practice_complete','assisted_complete') else 'remediate' if state in ('needs_support','needs_recheck') else 'diagnose'
        return {**base,'action':action,'concept_id':key,'assignment_id':node['assignment_id'],
                'session_id':memory.get(key,{}).get('latest_observation',{}).get('session_id') if action=='check' else None,
                'reason':'按有效学习证据选择可开始的目标；诊断后再决定是否需要讲解。'}
    return {**base,'action':'complete','reason':'路径内目标均有独立完成观察；这不是长期掌握保证。'}
