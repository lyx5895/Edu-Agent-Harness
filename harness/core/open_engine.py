"""Persistent task-oriented teaching runtime, independent of all example courses."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from harness.paths import ROOT
from harness.errors import Conflict
from harness.resources.images import load_image, normalize_image, store_image
from harness.llm.model import ModelUnavailable
from harness.llm.open_model import OpenModel
from harness.core.tasks import HINT_LEVELS, POLICIES, POLICY_VERSION, public_task
from harness.resources.toolbox import Toolbox, chunks, retrieve
from harness.grading.assessment import adjudicate
from harness.core.memory import summarize, goal_key
from harness.resources.knowledge import Knowledge
from harness.teaching import teacher
from harness.teaching import planning
from harness.teaching import preparation
from harness.grading.criteria import aggregate, bind_algebra
from harness.persistence.storage import Snapshot
from harness.persistence.replay import ReplayableModel, rerun, retry_stale, started as replay_started
from harness.core.pedagogy import check_contract, bind_check_contract
from harness.core.engine_feedback import FeedbackMixin
from harness.grading.engine_grading import GradingMixin
from harness.resources.engine_knowledge import KnowledgeMixin
from harness.teaching.engine_teacher import TeacherMixin
from harness.teaching.engine_planning import PlanningMixin
from harness.teaching.engine_preparation import PreparationMixin
from harness.core.evidence import dump, public_dimensions
# Preserve evidence helper imports used by existing callers.
from harness.core.evidence import ground as ground, loose_match as loose_match, verbatim_quote as verbatim_quote


class Turn:
    """Mutable working state of one operation, shared by the per-kind handlers."""
    def __init__(self,**fields):
        self.task=None;self.ctx={};self.o={};self.assessment={}
        self.__dict__.update(fields)


def state_diff(before, after):
    return {k:{'before':before.get(k),'after':after.get(k)} for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)}


class OpenEngine(FeedbackMixin, GradingMixin, KnowledgeMixin, TeacherMixin, PlanningMixin, PreparationMixin):
    """Session lifecycle and shared persistence for the feature mixins.

    Mixins group methods by responsibility; all writes use this runtime's
    transaction and idempotency helpers so cross-feature operations stay atomic.
    """
    def __init__(self,db=None,model=None,tools=None,policy=None,reviewer=None):
        self.policy=policy or os.getenv('OMNIEDU_POLICY',POLICY_VERSION)
        if self.policy not in POLICIES: raise ValueError(f'未知教学策略：{self.policy}')
        self.db=str(db or ROOT/'data/learning.sqlite')
        Path(self.db).parent.mkdir(parents=True,exist_ok=True)
        self.images=Path(self.db).parent/(Path(self.db).stem+'-images')
        self.model=ReplayableModel(model or OpenModel());self.tools=tools or Toolbox()
        if reviewer is None and os.getenv('OMNIEDU_REVIEW_BASE_URL'):
            reviewer=OpenModel(os.getenv('OMNIEDU_REVIEW_BASE_URL'),os.getenv('OMNIEDU_REVIEW_MODEL','qwen3.5-27b'))
        self.knowledge=Knowledge()
        self.reviewer=ReplayableModel(reviewer,'reviewer') if reviewer else None
        with self.connect() as c:
            teacher.initialize(c)
            planning.initialize(c)
            preparation.initialize(c)
            c.executescript('''
            CREATE TABLE IF NOT EXISTS learning_sessions(id TEXT PRIMARY KEY,learner TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_events(id INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_requests(session TEXT,request_id TEXT,digest TEXT,response TEXT,PRIMARY KEY(session,request_id));
            CREATE INDEX IF NOT EXISTS learning_event_session ON learning_events(session,kind,id);
            CREATE INDEX IF NOT EXISTS learning_session_learner ON learning_sessions(learner);
            ''')

    def connect(self):
        c=sqlite3.connect(self.db,timeout=310);c.row_factory=sqlite3.Row;return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        snapshot = Snapshot(c, locked=rerun())
        try:
            with self.model.budget() if hasattr(self.model, 'budget') else nullcontext():
                yield snapshot
            snapshot.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            snapshot.close()
            c.close()

    def image_payload(self,image_url,transcription,purpose):
        return {**store_image(image_url,self.images),'transcription':transcription,'purpose':purpose}

    def image_url(self,payload):
        return load_image(payload,self.images)

    def image_event(self,sid,event_id):
        with self.connect() as c:
            row=c.execute("SELECT payload FROM learning_events WHERE id=? AND session=? AND kind='image'",(event_id,sid)).fetchone()
        if not row: return None
        payload=json.loads(row['payload'])
        return {**payload,'image_url':self.image_url(payload)}

    def event(self,c,sid,kind,payload):
        return c.execute('INSERT INTO learning_events(session,kind,payload,at) VALUES(?,?,?,?)',
                         (sid,kind,dump(payload),time.time())).lastrowid

    def load(self,c,sid):
        r=c.execute('SELECT * FROM learning_sessions WHERE id=?',(sid,)).fetchone()
        if not r: raise ValueError('会话不存在')
        return r['learner'],json.loads(r['state'])

    def cached(self,c,sid,key,body):
        if not key or len(key)>100: raise ValueError('需要有效request_id')
        digest=hashlib.sha256(dump(body).encode()).hexdigest()
        r=c.execute('SELECT * FROM learning_requests WHERE session=? AND request_id=?',(sid,key)).fetchone()
        if r and r['digest']!=digest: raise Conflict('同一request_id不能提交不同内容')
        return digest,json.loads(r['response']) if r else None

    def remember(self,c,sid,key,digest,response):
        c.execute('INSERT INTO learning_requests VALUES(?,?,?,?)',(sid,key,digest,dump(response)))

    def build(self,request,sources,mode,previous=None):
        selected=retrieve(sources,request)
        # Planning may inspect the opening material, but this is not a relevance hit.
        retrieval_mode='matched' if selected else 'overview' if sources else 'none'
        if not selected: selected=sources[:2]
        task=self.model.plan(request,selected,mode,previous)
        if mode=='problem' and not previous: task['question']=request
        quote=task.get('source_quote','')
        if not selected:
            task['source_quote']=''
            task['citation_validation']={'status':'no_sources','discarded_model_quote':quote}
        elif quote and not any(quote in x['text'] for x in selected):
            raise ModelUnavailable('模型引用的材料原文无法核对，未创建任务，请重试。')
        cited=[x for x in selected if task.get('source_quote') and task['source_quote'] in x['text']] or selected
        task.update({'id':uuid.uuid4().hex,'phase':'check' if previous else 'teach',
            'source_ids':[x['id'] for x in selected], 'reference_origin':'model_generated_unverified','retrieval_mode':retrieval_mode,
            'source_pages':sorted({x['page'] for x in cited if 'page' in x})})
        return task

    @retry_stale
    def create(self,learner,request='',material='',mode='problem',image_url='',request_id=None,assignment_id=None,followup_id=None):
        if not learner.strip() or len(learner)>80: raise ValueError('请输入有效学生名称')
        if mode not in ('problem','learn'): raise ValueError('模式应为problem或learn')
        if len(request)>1600 or len(material)>100000: raise ValueError('题目上限1600字，材料上限100000字')
        if not request.strip() and not material.strip() and not image_url and not assignment_id and not followup_id: raise ValueError('请输入题目、学习目标、材料或图片')
        if not request.strip() and material.strip() and not image_url: mode='learn'
        key=request_id or ('followup:'+followup_id if followup_id else uuid.uuid4().hex)
        with self.transaction() as c:
            followup=planning.load_followup(c,followup_id) if followup_id else None
            if followup:
                if followup['learner']!=learner: raise ValueError('跟进任务不属于该学生')
                if followup['status'] in ('cancelled','needs_recheck'): raise Conflict('此任务已撤销或证据失效，请教师重新安排')
                if followup['session_id']:
                    owner,state=self.load(c,followup['session_id'])
                    return self.view(c,followup['session_id'],owner,state)
                assignment_id=followup['body']['assignment_id']
            assignment=teacher.load(c,assignment_id) if assignment_id else None
            if assignment:
                request=assignment['question']; material=assignment['material']; mode='problem'
            digest,old=self.cached(c,'create:'+learner,key,[request,material,mode,image_url,assignment_id,followup_id])
            if old: return old
            sid='open_'+uuid.uuid4().hex
            state={'version':0,'mode':mode,'request':request,'sources':chunks(material),'task':None,
                   'original':None,'pending_image':None,'pending_question':None,'assisted':False,
                   'failures':0,'hypotheses':[],'completed':False,'needs_review':False,'seen':[],
                   'scope':{k:assignment[k] for k in ('class_id','course_id','concept_id')} if assignment else {},
                   'followup_id':followup_id,'assignment_id':assignment_id,'teaching_note':assignment.get('teaching_note','') if assignment else '',
                   'allow_explanation':assignment.get('allow_explanation',True) if assignment else True}
            if assignment and image_url: raise ValueError('教师任务已有题目，不接受替换题目图片')
            if image_url:
                image_url=normalize_image(image_url)
                transcription=self.model.transcribe(image_url)
                state['pending_image']={**transcription,'purpose':'task'}
                self.event(c,sid,'image',self.image_payload(image_url,transcription,'task'))
            else:
                if assignment:
                    task={k:assignment[k] for k in ('domain','objective','question','rubric','reference','prerequisite')}
                    task.update(task_type=assignment.get('task_type','open'),check_requirements=assignment.get('check_requirements',[]),
                                check_assignment_ids=assignment.get('check_assignment_ids',[]))
                    task.update(id=uuid.uuid4().hex,phase='teach',source_ids=[x['id'] for x in state['sources']],
                                source_quote='',reference_origin=assignment.get('reference_origin','teacher_authored'),criteria=assignment.get('criteria',[]))
                    task.update(bind_algebra(task['question'],assignment.get('algebra_initial',''),assignment.get('algebra_kind','none')))
                else:
                    task=self.build(request or '根据材料提出一个理解检查问题',state['sources'],mode)
                state['task']=task;state['original']=deepcopy(task);state['seen']=[task['question']]
                state['original_sources']=deepcopy(state['sources'])
                if followup and followup['body']['kind'] in ('review','diagnostic'):
                    for row in c.execute('SELECT state FROM learning_sessions WHERE learner=?',(learner,)):
                        old_state=json.loads(row['state'])
                        if old_state.get('scope',{}).get('class_id')==assignment['class_id'] and task['question'].strip() in [q.strip() for q in old_state.get('seen',[])]:
                            raise Conflict('该检查题已被学生见过，请教师安排新题')
                    state['task']['phase']='check'
                if followup:
                    c.execute("UPDATE teaching_followups SET status='active',session_id=? WHERE id=?",(sid,followup_id))
                    planning.event(c,followup_id,'started',{'session_id':sid})
            c.execute('INSERT INTO learning_sessions VALUES(?,?,?)',(sid,learner,dump(state)))
            self.event(c,sid,'created',{'request':request,'mode':mode,'task':state['task']})
            result=self.view(c,sid,learner,state)
            self.remember(c,'create:'+learner,key,digest,result)
            return result

    def view(self,c,sid,learner,s):
        history=[]
        for r in c.execute('SELECT id,kind,payload FROM learning_events WHERE session=? ORDER BY id',(sid,)):
            p=json.loads(r['payload'])
            if r['kind'] in ('turn','correction'):
                if 'criteria_results' in p:p['criteria_results']=public_dimensions(p['criteria_results'])
                history.append({'id':r['id'],'type':r['kind'],**{k:p[k] for k in ('text','feedback','status','evidence','action','criteria_results') if k in p}})
        return {'session_id':sid,'learner':learner,'version':s['version'],'engine':'open-task-v1',
            'task':public_task(s['task']) if s['task'] else None,
            'pending_image':s['pending_image'],'pending_question':s['pending_question'],
            'completed':s['completed'],'needs_review':s['needs_review'],
            'scope':s.get('scope',{}),'assignment_id':s.get('assignment_id'),'followup_id':s.get('followup_id'),
            'source_count':len(s['sources']),'history':history}

    def session(self,sid):
        with self.connect() as c:
            learner,s=self.load(c,sid);return self.view(c,sid,learner,s)

    def context(self,c,sid,s,text):
        recent=[]
        for r in c.execute("SELECT id,payload FROM learning_events WHERE session=? AND kind='turn' ORDER BY id DESC LIMIT 4",(sid,)):
            p=json.loads(r['payload']);recent.append({'event_id':r['id'],
                **{k:str(p[k])[:180] for k in ('text','feedback','status','action') if k in p}})
        if s['task']['phase']=='check': recent=[]
        prior=[]
        if s['task']['phase'] != 'check':
            learner,_=self.load(c,sid)
            for row in c.execute('SELECT id,state FROM learning_sessions WHERE learner=? AND id!=?',(learner,sid)):
                old=json.loads(row['state'])
                if old['needs_review'] or not old['original']: continue
                # Exact objective or question match avoids importing unrelated subject evidence.
                if old.get('scope',{}).get('class_id') != s.get('scope',{}).get('class_id'): continue
                if old.get('scope',{}).get('course_id') != s.get('scope',{}).get('course_id'): continue
                if s.get('scope',{}).get('concept_id') and old.get('scope',{}).get('concept_id')!=s['scope']['concept_id']: continue
                same_goal=goal_key(old)==goal_key(s)
                if not same_goal and old['original']['question'] != s['original']['question']: continue
                for event in c.execute("SELECT id,payload FROM learning_events WHERE session=? AND kind='turn' ORDER BY id DESC LIMIT 2",(row['id'],)):
                    p=json.loads(event['payload'])
                    if p.get('assessment'):
                        prior.append({'session_id':row['id'],'event_id':event['id'],
                            'student_quote':p['assessment']['evidence_quote'][:120],
                            'hypothesis':p['assessment']['hypothesis'][:120],'certainty':'tentative'})
        image_id=None
        for row in c.execute("SELECT id,payload FROM learning_events WHERE session=? AND kind='image' ORDER BY id DESC",(sid,)):
            image_payload=json.loads(row['payload'])
            if (s.get('pending_image') and s['pending_image'].get('purpose')=='work') or (s['task']['phase']=='teach' and image_payload.get('purpose')=='task'):
                image_id=row['id'];break
        learner,_=self.load(c,sid)
        memory=[] if s['task']['phase']=='check' else [m for m in summarize(c,learner,exclude=sid) if m['goal_id']==goal_key(s)]
        dependencies=[]
        for item in memory:
            dependencies.extend(item.pop('source_sessions',[]))
            item['evidence']=[{**e,'student_quote':e['student_quote'][:100],'hypothesis':e['hypothesis'][:80]} for e in item['evidence'][-2:]]
            item['interventions']=[{**e,'feedback':e['feedback'][:180],'student_reply':e['student_reply'][:100]} for e in item['interventions'][-2:]]
        return {'memory_dependencies':dependencies,'image_evidence_id':image_id,'learning_memory':memory,'teacher_guidance':s.get('teaching_note','') if s['task']['phase']!='check' else '', 'prior_task_evidence':prior[-2:], 'history':list(reversed(recent)),'hypotheses':s['hypotheses'][-2:] if recent else [],
                'materials':retrieve(s['sources'],text+' '+s['task']['objective'])}


    @retry_stale
    def operation(self,sid,key,kind,body,version=None):
        started=replay_started()
        with self.transaction() as c:
            digest,old=self.cached(c,sid,key,[kind,body,version])
            if old: return old
            learner,s=self.load(c,sid)
            if version is not None and version!=s['version']: raise Conflict('会话已更新，请刷新后重试')
            if s['needs_review']: raise Conflict('教师已修订证据，请新建任务重新检查')
            text=body.get('text','').strip()
            if len(text)>1600: raise ValueError('单次作答上限1600字，请分步提交')
            before=deepcopy(s)
            t=Turn(c=c,sid=sid,learner=learner,s=s,kind=kind,body=body,text=text,
                   trace={'policy_version':self.policy,'operation':kind,'tools':[]},
                   payload={'text':text,'evidence':'interaction_only'})
            {'confirm_task':self._confirm_task,'image':self._image}.get(kind,self._teach)(t)
            payload,trace=t.payload,t.trace
            payload['task_id']=s['task']['id'] if s['task'] else None
            eid=self.event(c,sid,'turn',payload)
            planning.observe(c,sid,payload,eid)
            s['version']+=1
            trace.update(evidence_event_id=eid,state_diff=state_diff(before,s),state_version=s['version'],output=payload['feedback'],
                seconds=time.time()-started,model_calls=[r for r in getattr(self.model,'calls',[]) if r['at']>=started],
                budget={'max_model_calls':12,'max_repairs_per_structured_call':1})
            tid=self.event(c,sid,'trace',trace)
            c.execute('UPDATE learning_sessions SET state=? WHERE id=?',(dump(s),sid))
            result={k:payload[k] for k in ('feedback','status','evidence','action','criteria_results') if k in payload}
            if 'criteria_results' in result:result['criteria_results']=public_dimensions(result['criteria_results'])
            result.update(event_id=eid,trace_id=tid,session=self.view(c,sid,learner,s))
            self.remember(c,sid,key,digest,result)
            return result

    def _confirm_task(self,t):
        s=t.s
        if not s['pending_image'] or s['pending_image']['purpose']!='task': raise Conflict('没有待确认的题目图片')
        if not t.text: raise ValueError('请确认或修正图片中的题目')
        task=self.build(t.text,s['sources'],s['mode'])
        s.update(task=task,original=deepcopy(task),pending_image=None,seen=[task['question']],request=t.text)
        t.payload['feedback']='题目已确认。请提交你的尝试，或请求提示。'
        t.trace['tools']=['task.plan'];t.trace['generated_task']=task

    def _image(self,t):
        s=t.s
        if s['completed']: raise Conflict('当前任务已完成，请先开始检查或回到练习')
        if s['pending_image']: raise Conflict('请先确认已有图片')
        image_url=normalize_image(t.body.get('image_url'))
        transcription=self.model.transcribe(image_url)
        s['pending_image']={**transcription,'purpose':'work'}
        self.event(t.c,t.sid,'image',self.image_payload(image_url,transcription,'work'))
        t.payload['feedback']='请确认或修正识别出的作答，再进行判定。';t.trace['tools']=['image.transcribe']

    def _teach(self,t):
        s=t.s
        if not s['task']: raise Conflict('请先确认题目')
        if s['pending_image'] and t.kind!='confirm': raise Conflict('请先确认或修正图片转写')
        if t.kind=='confirm' and (not s['pending_image'] or s['pending_image']['purpose']!='work'): raise Conflict('没有待确认的作答图片')
        t.task=s['task'];t.ctx=self.context(t.c,t.sid,s,t.text);t.assessment={}
        t.trace['context']=t.ctx;t.trace['task_id']=t.task['id'];t.trace['tools'].append('materials.retrieve')
        memory=t.ctx.get('learning_memory',[])
        t.o={'kind':t.kind,'phase':t.task['phase'],'status':'unknown','failures':s['failures'],
             'prior_support_needed':any(m['state']=='needs_support' for m in memory),
             'fallbacks':s.get('feedback_fallbacks',0),
             'hints':s.get('hints_by_task',{}).get(t.task['id'],0)}
        handler={'check':self._check,'resume_practice':self._resume,'attempt':self._attempt,'confirm':self._attempt,
                 'reply':self._reply,'hint':self._help,'explain':self._help}.get(t.kind)
        if handler is None: raise ValueError('未知教学操作')
        handler(t)
        self._decide(t)

    def _check(self,t):
        c,s,task,trace=t.c,t.s,t.task,t.trace
        if task['phase']=='check': raise Conflict('请先完成当前独立检查')
        if not s['completed']: raise Conflict('先完成当前练习，再创建独立检查')
        seen=list(s['seen'])
        for row in c.execute('SELECT state FROM learning_sessions WHERE learner=?',(t.learner,)):
            old_state=json.loads(row['state'])
            if goal_key(old_state)==goal_key(s): seen.extend(old_state.get('seen',[]))
        bank=s['original'].get('check_assignment_ids',[])
        if bank:
            new,review=self._bank_check(c,s,bank,seen)
            trace.update(generated_task=new,review=review,check_source='teacher_bank')
        else:
            new,review,generations=self._generated_check(s,task,seen)
            trace['check_generation_attempts']=generations
        s.update(task=new,completed=False,assisted=False,pending_question=None,failures=0)
        s['seen'].append(new['question']);t.payload['feedback']='新的独立检查已准备好，请独立作答。'
        trace.update(generated_task=new,review=review);trace['tools'].append('task.teacher_bank' if bank else 'task.generate_and_review')

    def _bank_check(self,c,s,bank,seen):
        used={re.sub(r'\s','',q) for q in seen}
        candidates=[(key,teacher.load(c,key)) for key in bank]
        selected=next(((key,a) for key,a in candidates if re.sub(r'\s','',a['question']) not in used),None)
        if selected is None:raise ModelUnavailable('教师检查题已用完，请教师补充新题；原任务保持不变。')
        bank_key,a=selected
        new={k:deepcopy(a[k]) for k in ('domain','objective','question','rubric','reference','prerequisite','criteria','task_type','check_requirements')}
        new.update(bind_algebra(a['question'],a.get('algebra_initial',''),a.get('algebra_kind','none')))
        new.update(id=uuid.uuid4().hex,phase='check',source_quote='',source_ids=[],
                   reference_origin=a.get('reference_origin','teacher_authored'),check_assignment_id=bank_key)
        s['sources']=chunks(a['material'])
        new['source_ids']=[x['id'] for x in s['sources']]
        return new,{'acceptable':True,'reason':'教师审核题库'}

    def _generated_check(self,s,task,seen):
        previous={'objective':task['objective'],'question':task['question'],'avoid_questions':seen[-12:],
                  'contract':check_contract(s['original'])}
        generations=[]
        for _ in range(2):
            new=self.build(task['objective'],s['sources'],'learn',previous)
            new['objective']=task['objective']
            new['domain']=task['domain']
            try:
                bind_check_contract(new, s['original'])
            except ValueError as error:
                review={'acceptable':False,'reason':str(error)}
                generations.append({'candidate':new,'review':review,'reference_assessment':None})
                previous['rejected_candidate']={'question':new['question'],'reason':str(error)}
                continue
            duplicate=re.sub(r'\s','',new['question']) in [re.sub(r'\s','',q) for q in seen]
            review={'acceptable':False,'reason':'重复旧题，必须更换数据、情境或考察角度。'} if duplicate else self.model.review(new,dump(new),'check_candidate')
            key_assessment=None
            if review['acceptable']:
                new['reference_origin']='model_generated_reviewed_same_model'
                verified,_=self.tools.verify(new,new['reference'])
                key_context={'history':[],'hypotheses':[],'prior_task_evidence':[],
                    'materials':retrieve(s['sources'],new['reference']+' '+new['objective'])}
                key_assessment=self.model.assess(new,new['reference'],key_context,verified)
                key_ok=adjudicate(new,new['reference'],verified,key_assessment)[0]=='correct'
                if key_ok and new.get('criteria'):
                    key_ok=aggregate(self.grade_dimensions(new,new['reference'],key_context))=='correct'
                if not key_ok:
                    review={'acceptable':False,'reason':'参考答案未通过评分：'+key_assessment['observation']}
            generations.append({'candidate':new,'review':review,'reference_assessment':key_assessment})
            if review['acceptable']: break
            previous['rejected_candidate']={'question':new['question'],'reference':new['reference'],'reason':review['reason']}
        else:
            raise ModelUnavailable('两次生成的新检查题均未通过审核，未更新状态，请重试。')
        new['reference_origin']='model_generated_reviewed_same_model'
        return new,review,generations

    def _resume(self,t):
        s=t.s
        s['sources']=deepcopy(s.get('original_sources',s['sources']))
        s.update(task=deepcopy(s['original']),completed=False,assisted=True,pending_question=None,failures=0)
        t.payload['feedback']='已回到原任务，可以继续提问或请求讲解。'
        t.o['phase']='teach';t.o['status']='unknown'


    def _reply(self,t):
        s,text=t.s,t.text
        if t.task['phase']=='check': raise Conflict('独立检查时只评估作答')
        if not s['pending_question']: raise Conflict('没有待回答的追问，请提交作答')
        if not text: raise ValueError('请输入回答')
        interpreted=self.model.interpret_reply({'question':s['pending_question'],
            'student_reply':text,'rule':t.task['rubric'],'history':t.ctx})
        if not interpreted['evidence_quote'] or interpreted['evidence_quote'] not in text:
            raise ModelUnavailable('回答证据无法核对，未更新记录。')
        t.o['outcome']=interpreted['outcome'];t.trace['interpretation']=interpreted
        s['hypotheses']=(s['hypotheses']+[dict(interpreted,evidence_quote=interpreted['evidence_quote'][:200],certainty='tentative')])[-3:]
        t.payload['evidence']='dialogue_only'

    def _help(self,t):
        if t.task['phase']=='check': raise Conflict('检查中不提供帮助，请回到原任务')
        if t.kind=='explain' and not t.s.get('allow_explanation',True): raise Conflict('教师为本任务关闭了完整讲解，请请求提示')

    def _decide(self,t):
        s,payload=t.s,t.payload
        unproductive=t.task['phase']=='teach' and t.kind in ('attempt','confirm','reply') and t.o.get('status')!='correct'
        t.o['stalls']=s.get('stalls',0)+1 if unproductive else 0
        decision=POLICIES[self.policy](t.o)
        s['stalls']=0 if decision['action'] in [level for level,_ in HINT_LEVELS]+['explain'] else t.o['stalls']
        if t.kind=='resume_practice': decision={'action':'resume_practice','reason':'学生选择回到原任务继续学习。'}
        t.trace.update(input=t.o,decision=decision)
        payload['action']=decision['action']
        if decision['action'] in [level for level,_ in HINT_LEVELS]:
            counts=s.setdefault('hints_by_task',{})
            counts[t.task['id']]=counts.get(t.task['id'],0)+1
        if 'feedback' not in payload:
            output,validation=self.feedback(t.task,decision['action'],t.text,t.ctx,t.assessment)
            payload['feedback']=output;t.trace['output_validation']=validation
            payload['delivered_action']=validation.get('delivered_action',decision['action'])
            generic=validation['fallback'] and payload['delivered_action']!='locate_template'
            s['feedback_fallbacks']=s.get('feedback_fallbacks',0)+1 if generic else 0
            s['assisted']=True;s['completed']=False
            s['pending_question']=output if decision['action'] not in ('self_correct','explain') else None
