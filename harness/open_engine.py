"""Persistent task-oriented teaching runtime, independent of all example courses."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from .errors import Conflict
from .images import load_image, normalize_image, store_image
from .model import ModelUnavailable
from .open_model import OpenModel
from .tasks import HINT_LEVELS, POLICIES, POLICY_VERSION, language, public_task, sentences
from .toolbox import Toolbox, chunks, retrieve
from .assessment import adjudicate
from .memory import summarize, goal_key
from . import teacher, planning, preparation
from .criteria import tool_grade, aggregate, bind_algebra
from .storage import Snapshot
from .replay import ReplayableModel, rerun, retry_stale, started as replay_started
from .pedagogy import check_contract, bind_check_contract


def dump(x): return json.dumps(x,ensure_ascii=False,sort_keys=True)


FALLBACKS={
    'zh':{'default':'请指出你最不确定的部分，并说明你目前的思路？',
          'probe':'你得出这个回答时，依据的规则或材料是什么？',
          'reading':'请指出你用到的原文位置，并说说它如何支持你的判断？',
          'writing':'你最想表达的观点是什么，哪一句还没有表达清楚？',
          'experiment':'你记录了什么现象或数据，它能支持哪一步判断？',
          'programming':'请给一个你不确定的输入，并描述程序现在与预期的输出？',
          'concept':'你能给出一个例子，说明你现在怎样理解这个概念吗？',
          'problem_solving':'你最不确定的是哪一步，这一步使用了什么依据？',
          'self_correct':'请根据你刚才说明的思路自行修正作答，并写出依据。',
          'self_correct_retry':'请按照你刚才说明的思路重新作答，再提交检查。',
          'explain_failed':'这次生成的讲解未通过检查，请重试或请教师核对。',
          'teacher_review':'当前自动反馈需要教师复核。请保留你的作答和疑问，等待老师指导。',
          'locate':'请再检查这一处：「{quote}」。这一步依据的是什么，结果成立吗？',
          'passed_tool':'本次作答通过工具验证。','passed_model':'本次作答通过模型评分规则检查（仍可请教师复核）。',
          'offer_check':'可以开始一题新的独立检查。','check_failed':'本次独立检查尚未通过。可继续作答，或回到原任务请求帮助。'},
    'en':{'default':'Which part are you least sure about, and what is your current reasoning?',
          'probe':'What rule or information did you rely on to get this answer?',
          'reading':'Which part of the text did you use, and how does it support your conclusion?',
          'writing':'What is the main point you want to make, and which sentence does not express it clearly yet?',
          'experiment':'What observation or data did you record, and which step of your reasoning does it support?',
          'programming':'Give one input you are unsure about and describe the actual versus expected output?',
          'concept':'Can you give an example that shows how you currently understand this concept?',
          'problem_solving':'Which step are you least sure about, and what did you base that step on?',
          'self_correct':'Please revise your answer yourself following the reasoning you just described, and show why.',
          'self_correct_retry':'Please redo the problem following the reasoning you just described, then submit it again.',
          'explain_failed':'The generated explanation did not pass checks; please retry or ask your teacher.',
          'teacher_review':'This feedback needs a teacher to review it. Please keep your work and questions; your teacher will follow up.',
          'locate':'Take another look at this part: "{quote}". What is this step based on, and does it hold?',
          'passed_tool':'Your answer passed the tool check. ','passed_model':'Your answer passed the rubric check by the model (a teacher may still review it). ',
          'offer_check':'You can start a new independent check.','check_failed':'This independent check is not passed yet. Keep working, or go back to the original task for help.'}}


def verbatim_quote(quote, text):
    """Return an exact substring of text to store as evidence, or None.
    A quote whose tail was garbled near the length cap keeps its verbatim prefix."""
    if not quote: return None
    if quote in text: return quote
    trimmed=quote.rstrip().rstrip('.…').rstrip()
    low,high=0,len(trimmed)
    while low<high:
        mid=(low+high+1)//2
        if trimmed[:mid] in text: low=mid
        else: high=mid-1
    return trimmed[:low] if low>=max(12,int(.8*len(trimmed))) else None


class Turn:
    """Mutable working state of one operation, shared by the per-kind handlers."""
    def __init__(self,**fields):
        self.task=None;self.ctx={};self.o={};self.assessment={}
        self.__dict__.update(fields)


def state_diff(before, after):
    return {k:{'before':before.get(k),'after':after.get(k)} for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)}


def ground(assessment, text):
    """Bind evidence to the student's own words: the verbatim quote, else the numbered sentence the model chose."""
    quote=verbatim_quote(assessment.get('evidence_quote',''),text)
    if quote is None:
        spans=sentences(text);n=assessment.get('evidence_sentence',0)
        if 1<=n<=len(spans): quote=spans[n-1]
    return quote


def review_risks(p):
    risks=[]
    if p.get('status') in ('incorrect','incomplete'): risks.append('negative_judgment')
    if p.get('assessment',{}).get('confidence')!='high': risks.append('low_confidence')
    if p.get('evidence')=='independent_model_assessed': risks.append('independent_by_model_only')
    if str(p.get('task',{}).get('reference_origin','')).startswith('model_generated'): risks.append('model_reference')
    if any(x.get('source')=='model' and x.get('status')=='unknown' for x in p.get('criteria_results',[])): risks.append('criterion_undetermined')
    return risks


def public_dimensions(rows):
    # Assessment observations can contain the private reference; keep them teacher-only.
    return [{k:r[k] for k in ('id','description','concept_id','status','source') if k in r} for r in rows]


class OpenEngine:
    def __init__(self,db=None,model=None,tools=None,policy=None):
        self.policy=policy or os.getenv('OMNIEDU_POLICY',POLICY_VERSION)
        if self.policy not in POLICIES: raise ValueError(f'未知教学策略：{self.policy}')
        self.db=str(db or Path(__file__).resolve().parents[1]/'data/learning.sqlite')
        Path(self.db).parent.mkdir(parents=True,exist_ok=True)
        self.images=Path(self.db).parent/(Path(self.db).stem+'-images')
        self.model=ReplayableModel(model or OpenModel());self.tools=tools or Toolbox()
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
        task.update({'id':uuid.uuid4().hex,'phase':'check' if previous else 'teach',
            'source_ids':[x['id'] for x in selected], 'reference_origin':'model_generated_unverified','retrieval_mode':retrieval_mode})
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

    def feedback(self,task,action,text,context,assessment):
        lines=FALLBACKS[language(task.get('question','')+text)]
        if action=='teacher_review':
            return lines['teacher_review'], {'candidate':None,'review':{},'reasons':[],'fallback':False,'delivered_action':'teacher_review'}
        if action=='self_correct':
            return lines['self_correct'], {'candidate':None,'review':{'acceptable':True,'reason':'服务器只邀请自行作答，不提供答案'},'reasons':[],'fallback':False,'source':'policy_template'}
        candidate=self.model.respond(task,action,text,context,assessment)
        english=language(task.get('question','')+text)=='en'
        if english and candidate and language(candidate)=='zh': candidate=self.model.translate(candidate)
        review=self.model.review(task,candidate,action,text)
        reasons=[]
        if not candidate or len(candidate)>1200: reasons.append('length')
        if action=='probe':
            if candidate.count('？')+candidate.count('?') != 1 or not candidate.endswith(('？','?')) or any(x in candidate for x in ('。','!','！','\n')):
                reasons.append('probe_must_be_one_question')
        if action=='locate' and (candidate.count('？')+candidate.count('?')!=1 or not candidate.rstrip().endswith(('？','?'))):
            reasons.append('locate_must_end_with_one_question')
        if re.search(r'你已经掌握|已通过独立检查|系统指令',candidate): reasons.append('authority_claim')
        if english and language(candidate)=='zh': reasons.append('language_mismatch')
        answer=re.sub(r'\s','',task['reference'])
        if action!='explain' and len(answer)>=3 and answer in re.sub(r'\s','',candidate): reasons.append('reference_disclosure')
        if not review['acceptable']: reasons.append('model_review')
        lines=FALLBACKS[language(task.get('question','')+text)]
        fallback=lines['probe'] if action=='probe' else lines['default']
        if action!='explain':fallback=lines.get(task.get('task_type'),fallback)
        if action=='self_correct': fallback=lines['self_correct_retry']
        if action=='explain': fallback=lines['explain_failed']
        delivered='clarify' if reasons else action
        if action=='locate' and assessment.get('evidence_quote'):
            quote=assessment['evidence_quote'].strip().rstrip('.。')
            if len(quote)>120: quote=(quote[:120].rsplit(' ',1)[0] if ' ' in quote[:120] else quote[:120])+'…'
            fallback=lines['locate'].format(quote=quote)
            if reasons: delivered='locate_template'
        return (fallback if reasons else candidate),{'candidate':candidate,'review':review,'reasons':reasons,'fallback':bool(reasons),'delivered_action':delivered}

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

    def _attempt(self,t):
        c,s,task,text,trace,payload,o=t.c,t.s,t.task,t.text,t.trace,t.payload,t.o
        if s['completed']: raise Conflict('本任务已完成，请创建独立检查或继续学习')
        if not text: raise ValueError('请填写作答')
        verification,tool_results=self.tools.verify(task,text)
        assessment_ctx=dict(t.ctx)
        if t.ctx.get('image_evidence_id'):
            img=c.execute('SELECT payload FROM learning_events WHERE id=?',(t.ctx['image_evidence_id'],)).fetchone()
            assessment_ctx['image_url']=self.image_url(json.loads(img[0]))
        assessment,quote,dimensions=self._grade(task,text,assessment_ctx,verification,trace)
        if quote is None:
            # An ungrounded judgment is not evidence; keep the attempt and route it to a teacher.
            trace['rejected_assessment']=assessment
            assessment={**assessment,'status':'unknown','confidence':'low','evidence_quote':'',
                        'observation':'模型判定未能引用学生原文，已转教师复核。'}
            payload['quote_unverified']=True
        else: assessment['evidence_quote']=quote
        t.assessment=assessment
        o['grounded']=quote is not None and assessment['status'] in ('incorrect','incomplete')
        status,source=adjudicate(task,text,verification,assessment)
        if task.get('criteria'):
            payload['criteria_results']=dimensions
            dimension_status=aggregate(dimensions)
            payload['whole_task_status']=status
            payload['dimension_status']=dimension_status
            payload['rubric_conflict']=dimension_status=='correct' and status!='correct'
            # Dimension success cannot erase an uncovered whole-task requirement.
            status=aggregate([{'status':status},{'status':dimension_status}])
            source='model_rubric'
        payload['grading_conflict']=verification['status'] in ('correct','incorrect') and assessment['status'] in ('correct','incorrect') and verification['status']!=assessment['status']
        if re.fullmatch(r'(我)?(懂了|会了|知道了|明白了|学会了)[。！! ]*',text): status='unknown'
        o['status']=status
        payload.update(status=status,assessment=assessment,verification=verification,source=source,
            task=deepcopy(task),assisted=s['assisted'])
        trace['tools']+=tool_results
        lines=FALLBACKS[language(task['question']+text)]
        if status=='correct':
            s['completed']=True;s['pending_question']=None
            payload['evidence']=('independent_verified' if source in ('sympy','rubric_tools') else 'independent_model_assessed') if task['phase']=='check' else ('assisted_complete' if s['assisted'] else 'practice_complete')
            payload['feedback']=lines['passed_tool' if source in ('sympy','rubric_tools') else 'passed_model']
            if task['phase']=='teach': payload['feedback']+=lines['offer_check']
        elif task['phase']=='check':
            payload['feedback']=lines['check_failed']
        else:
            if status in ('incorrect','incomplete'): s['failures']+=1
            o['failures']=s['failures']
            s['hypotheses']=(s['hypotheses']+[{'hypothesis':assessment['hypothesis'],
                'quote':quote or '','certainty':'tentative'}])[-3:]
        s['pending_image']=None

    def _grade(self,task,text,ctx,verification,trace):
        with ThreadPoolExecutor(max_workers=1) as pool:
            graded=pool.submit(copy_context().run,self.grade_dimensions,task,text,ctx) if task.get('criteria') else None
            assessment=self.model.assess(task,text,ctx,verification)
            quote=ground(assessment,text)
            if quote is None:
                trace['rejected_assessment']=assessment
                assessment=self.model.assess(task,text,{**ctx,'quote_feedback':
                    '上次evidence_quote不是学生原文且evidence_sentence无效，请填写student_sentences中的编号并逐字复制该句。'},verification)
                quote=ground(assessment,text)
            return assessment,quote,(graded.result() if graded else None)

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
        decision=POLICIES[self.policy](t.o)
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
            s['feedback_fallbacks']=s.get('feedback_fallbacks',0)+1 if validation['fallback'] else 0
            s['assisted']=True;s['completed']=False
            s['pending_question']=output if decision['action'] not in ('self_correct','explain') else None

    def traces(self,sid):
        with self.connect() as c:
            self.load(c,sid)
            return [dict(json.loads(r['payload']),event_id=r['id']) for r in c.execute(
                "SELECT id,payload FROM learning_events WHERE session=? AND kind='trace' ORDER BY id",(sid,))]

    def replay(self,sid):
        checks=[]
        for t in self.traces(sid):
            if 'input' not in t: continue
            decision=POLICIES.get(t['policy_version'],lambda _:None)(t['input'])
            if t['operation']=='resume_practice': decision={'action':'resume_practice','reason':'学生选择回到原任务继续学习。'}
            checks.append({'event_id':t['event_id'],'matches':decision==t['decision']})
        return {'passed':all(t['matches'] for t in checks),'turns':checks,'model_called':False,
                'scope':'deterministic policy replay, not regeneration of model content'}

    def report(self,learner=None):
        with self.connect() as c:
            sessions=c.execute('SELECT * FROM learning_sessions'+(' WHERE learner=?' if learner else ''),
                               (learner,) if learner else ()).fetchall()
            reports=[]
            for row in sessions:
                turns=[];corrections={}
                events=c.execute('SELECT * FROM learning_events WHERE session=? ORDER BY id',(row['id'],)).fetchall()
                for e in events:
                    p=json.loads(e['payload'])
                    if e['kind']=='correction': corrections[p['event_id']]=p
                for e in events:
                    p=json.loads(e['payload'])
                    if e['kind']=='turn':
                        p['event_id']=e['id']
                        if e['id'] in corrections:
                            p['original_status']=p.get('status');p['status']=corrections[e['id']]['status']
                            p['original_evidence']=p['evidence'];p['evidence']='teacher_revised_needs_recheck'
                            p['correction']=corrections[e['id']]
                        turns.append(p)
                s=json.loads(row['state'])
                reports.append({'session_id':row['id'],'learner':row['learner'],'task':s['task'],
                    'needs_review':s['needs_review'],'turns':turns})
            return reports

    @retry_stale
    def correct(self,sid,event_id,status,reason,key):
        if status not in ('correct','incorrect','incomplete','unknown') or not reason.strip(): raise ValueError('需有效判定及依据')
        with self.transaction() as c:
            digest,old=self.cached(c,sid,key,['correction',event_id,status,reason])
            if old:return old
            _,s=self.load(c,sid)
            r=c.execute("SELECT payload FROM learning_events WHERE id=? AND session=? AND kind='turn'",(event_id,sid)).fetchone()
            if not r or 'status' not in json.loads(r['payload']): raise ValueError('找不到可修订的作答')
            result={'event_id':event_id,'status':status,'reason':reason}
            self.event(c,sid,'correction',result);s['needs_review']=True;s['version']+=1
            c.execute('UPDATE learning_sessions SET state=? WHERE id=?',(dump(s),sid))
            affected={sid}
            changed=True
            while changed:
                changed=False
                for row in c.execute('SELECT id,state FROM learning_sessions'):
                    if row['id'] in affected: continue
                    for tr in c.execute("SELECT payload FROM learning_events WHERE session=? AND kind='trace'",(row['id'],)):
                        ctx=json.loads(tr['payload']).get('context',{})
                        refs=list(ctx.get('prior_task_evidence',[]))+[{'session_id':x} for x in ctx.get('memory_dependencies',[])]
                        for mem in ctx.get('learning_memory',[]): refs.extend(mem.get('evidence',[])); refs.extend(mem.get('interventions',[]))
                        if any(ref.get('session_id') in affected for ref in refs):
                            state=json.loads(row['state']);state['needs_review']=True;state['version']+=1
                            c.execute('UPDATE learning_sessions SET state=? WHERE id=?',(dump(state),row['id']))
                            self.event(c,row['id'],'memory_invalidated',{'source_session':sid,'source_event':event_id})
                            affected.add(row['id']);changed=True;break
            planning.invalidate(c,affected)
            result['invalidated_sessions']=sorted(affected)
            self.remember(c,sid,key,digest,result);return result


    @retry_stale
    def publish_assignment(self, data):
        with self.transaction() as c:
            return teacher.publish(c, data)

    def learning_memory(self, learner, class_id=None):
        with self.connect() as c:
            return summarize(c, learner, {'class_id':class_id} if class_id else None)

    def dashboard(self, class_id=None):
        with self.connect() as c:
            learners=[r[0] for r in c.execute('SELECT DISTINCT learner FROM learning_sessions')]
            rows=[]
            for learner in learners:
                for memory in summarize(c,learner,{'class_id':class_id} if class_id else None):
                    rows.append({'learner':learner,**memory})
            pending=[]
            for row in c.execute("SELECT e.id,e.session,e.payload,s.state,s.learner FROM learning_events e JOIN learning_sessions s ON e.session=s.id WHERE e.kind='turn' ORDER BY e.id DESC"):
                state=json.loads(row['state']);p=json.loads(row['payload'])
                if class_id and state.get('scope',{}).get('class_id')!=class_id: continue
                corrected=c.execute("SELECT payload FROM learning_events WHERE session=? AND kind IN ('correction','review_confirmed')",(row['session'],)).fetchall()
                if any(json.loads(x[0])['event_id']==row['id'] for x in corrected): continue
                urgent=p.get('status')=='unknown' or p.get('grading_conflict') or p.get('rubric_conflict') or p.get('delivered_action') in ('clarify','teacher_review')
                risks=review_risks(p) if p.get('source')=='model_rubric' else []
                sampled=p.get('source')=='model_rubric' and int(hashlib.sha256(f"{row['session']}:{row['id']}".encode()).hexdigest(),16)%10==0
                if urgent or risks or sampled:
                    pending.append({'session_id':row['session'],'event_id':row['id'],'learner':row['learner'],
                        'status':p.get('status'),'text':p.get('text'),'feedback':p.get('feedback'),
                        'priority':'urgent' if urgent else 'risk' if risks else 'sample','risks':risks,
                        'criteria_results':p.get('criteria_results',[])})
            groups={}
            for row in rows:
                if row['state'] not in ('needs_support','needs_recheck'):continue
                last=row['evidence'][-1] if row['evidence'] else {}
                failures=[x for x in last.get('criteria',[]) if x['status']!='correct'] or [{'id':'overall','description':'整体作答需复查'}]
                for failure in failures:
                    key=(row['scope'].get('class_id'),row['scope'].get('course_id'),row['scope'].get('concept_id',row['goal_id']),failure['id'])
                    group=groups.setdefault(key,{'scope':row['scope'],'objective':row['objective'],'criterion':failure['description'],'learners':[],'examples':[]})
                    group['learners'].append(row['learner'])
                    if last:group['examples'].append(last)
            pending.sort(key=lambda p:({'urgent':0,'risk':1}.get(p['priority'],2),-len(p['risks'])))
            return {'learning':rows,'review_queue':pending[:100],'support_groups':list(groups.values()),
                    'followups':planning.list_followups(c,class_id),
                    'limits':'教师工作台；证据状态不是掌握概率；复习间隔按连续独立通过次数取1/3/7/14/30天，是规则建议而非遗忘模型。'}


    def grade_dimensions(self,task,text,context):
        from .criteria import GRADERS
        results=[];pending=[]
        for criterion in task['criteria']:
            result=tool_grade(criterion,{**task,'material':'\n'.join(x['text'] for x in context.get('materials',[]))},text)
            if criterion['kind'] not in GRADERS and criterion['kind'] != 'model':
                result['reason']='未安装该评分插件：'+criterion['kind']
            elif result['status']=='unknown' and criterion['kind'] not in ('source_quote','word_count') and hasattr(self.model,'assess_criterion'):
                pending.append((criterion,result))
            results.append(result)
        if pending:
            with ThreadPoolExecutor(max_workers=len(pending)) as pool:
                futures=[(result,pool.submit(copy_context().run,self.model.assess_criterion,task,criterion,text,context))
                         for criterion,result in pending]
                for result,future in futures:
                    assessment=future.result()
                    quote=ground(assessment,text)
                    valid=quote is not None and assessment['confidence']=='high'
                    result.update(status=assessment['status'] if valid else 'unknown',source='model',
                                  reason=assessment['observation'],evidence_quote=quote or '')
        return results

    @retry_stale
    def publish_path(self,data):
        with self.transaction() as c: return planning.publish_path(c,data)

    def next_step(self,path_id,learner):
        with self.connect() as c: return planning.next_step(c,path_id,learner)

    @retry_stale
    def assign_followups(self,data,request_id):
        with self.transaction() as c:
            digest,old=self.cached(c,'followups:'+data['class_id'],request_id,data)
            if old is not None:return old
            result=planning.assign(c,data)
            self.remember(c,'followups:'+data['class_id'],request_id,digest,result)
            return result

    def followups(self,class_id=None,learner=None):
        with self.connect() as c: return planning.list_followups(c,class_id,learner)

    @retry_stale
    def revise_followup(self,key,action,due_at,reason,request_id):
        if not reason.strip():raise ValueError('请填写调整原因')
        with self.transaction() as c:
            digest,old=self.cached(c,'followup:'+key,request_id,[action,due_at,reason])
            if old is not None:return old
            result=planning.revise(c,key,action,due_at,reason)
            self.remember(c,'followup:'+key,request_id,digest,result)
            return result


    @retry_stale
    def confirm_review(self,sid,event_id,reason,request_id):
        if not reason.strip():raise ValueError('请填写复核依据')
        with self.transaction() as c:
            digest,old=self.cached(c,sid,request_id,['review',event_id,reason])
            if old is not None:return old
            _,s=self.load(c,sid)
            row=c.execute("SELECT payload FROM learning_events WHERE session=? AND id=? AND kind='turn'",(sid,event_id)).fetchone()
            if not row:raise ValueError('找不到作答')
            payload=json.loads(row['payload'])
            if s['needs_review'] or payload.get('status') not in ('correct','incorrect','incomplete'):
                raise Conflict('失效或不确定判定需要修订，不能直接确认')
            result={'event_id':event_id,'reason':reason,'status':payload['status']}
            self.event(c,sid,'review_confirmed',result)
            self.remember(c,sid,request_id,digest,result)
            return result


    @retry_stale
    def save_pack(self, data, request_id, pack_id=None, version=None):
        with self.transaction() as c:
            scope='pack:'+pack_id if pack_id else 'packs:'+data['class_id']
            digest,old=self.cached(c,scope,request_id,['save',data,version])
            if old is not None:return old
            result=preparation.save(c,data,pack_id,version)
            self.remember(c,scope,request_id,digest,result)
            return result

    @retry_stale
    def generate_pack(self, data, request_id):
        body=preparation.GeneratePack.model_validate(data).model_dump()
        with self.transaction() as c:
            scope='packs:'+body['class_id']
            digest,old=self.cached(c,scope,request_id,['generate',body])
            if old is not None:return old
            items=[]
            for goal in body['goals']:
                request=goal['objective']+'\n教学要求：'+dump(goal)
                draft=self.build(request,chunks(body['material']),'learn')
                item={k:draft[k] for k in ('question','rubric','reference','prerequisite')}
                item.update(goal,class_id=body['class_id'],material=body['material'],
                            criteria=draft.get('criteria',[]),reference_origin='model_generated_unverified')
                # Generated drafts become teacher-reviewed only upon explicit publication.
                items.append(item)
            result=preparation.save(c,dict(class_id=body['class_id'],title=body['title'],items=items))
            preparation.event(c,result['pack_id'],'model_drafted',{'requires_teacher_review':True})
            self.remember(c,scope,request_id,digest,result)
            return result

    def packs(self,class_id):
        with self.connect() as c:
            return [preparation.load(c,r['id']) for r in c.execute(
                'SELECT id FROM preparation_packs WHERE class_id=? ORDER BY rowid DESC',(class_id,))]

    def load_pack(self,pack_id):
        with self.connect() as c:return preparation.load(c,pack_id)

    @retry_stale
    def publish_pack(self,pack_id,version,distribution,request_id,edited_body=None):
        with self.transaction() as c:
            scope='pack:'+pack_id
            digest,old=self.cached(c,scope,request_id,['publish',version,distribution,edited_body])
            if old is not None:return old
            if edited_body is not None:
                saved=preparation.save(c,edited_body,pack_id,version)
                version=saved['version']
            result=preparation.publish(c,pack_id,version,distribution)
            self.remember(c,scope,request_id,digest,result)
            return result

    def inbox(self,class_id,learner):
        # Do not expose answer keys, rubrics, draft packs or unseen check questions.
        with self.connect() as c:
            result=[]
            for f in planning.list_followups(c,class_id,learner):
                a=teacher.load(c,f['body']['assignment_id'])
                result.append({'followup_id':f['id'],'course_id':a['course_id'],'domain':a['domain'],
                    'objective':a['objective'],'task_type':a.get('task_type','open'),'status':f['status'],
                    'due_at':f['due_at'],'session_id':f['session_id']})
            return result
