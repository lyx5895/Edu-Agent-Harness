import base64
import json
import os
from pathlib import Path
import uuid

import gradio as gr
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .course import SKILLS
from .errors import Conflict
from .gateway import Gateway
from .images import file_to_data_url
from .materials import read_material
from .model import ModelUnavailable
from .teacher import Assignment
from .planning import Curriculum, FollowupBatch
from .preparation import Pack, GeneratePack, Distribution
from .pedagogy import PROFILES
from typing import Literal

engine = Gateway(db=os.getenv('HARNESS_DB'))
app = FastAPI(title='OmniEdu Teaching Harness', version='0.8.0')


@app.middleware('http')
async def classroom_access(request, call_next):
    from .access import authorize
    return await authorize(request,call_next,engine)


class NewSession(BaseModel):
    learner: str = Field(min_length=1,max_length=80)
    skill: str | None = None
    task: str = Field(default='',max_length=1600)
    material: str = Field(default='',max_length=100000)
    mode: str = 'problem'
    image_url: str = Field(default='',max_length=12_000_000)
    request_id: str | None = None
    assignment_id: str | None = None
    followup_id: str | None = None


class Operation(BaseModel):
    request_id: str = Field(min_length=1,max_length=100)
    version: int | None = None
    text: str = Field(default='',max_length=3000)
    image_url: str = Field(default='',max_length=12_000_000)


class Correction(BaseModel):
    request_id: str = Field(min_length=1,max_length=100)
    status: str
    reason: str = Field(min_length=1,max_length=1000)


def call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except ModelUnavailable as e:
        raise HTTPException(503,str(e))
    except Conflict as e:
        raise HTTPException(409,str(e))
    except ValueError as e:
        raise HTTPException(400,str(e))


@app.get('/health')
def health(): return {'ready':True,'mode':'local-demo','model_base_url':engine.model.base_url}


@app.get('/api/skills')
def skills(): return list(SKILLS.values())


@app.post('/api/sessions')
def create_session(req:NewSession):
    if req.skill:
        if req.task or req.material or req.image_url: raise HTTPException(400,'示例课程与自定义任务请分开创建')
        return call(engine.create,req.learner,req.skill)
    return call(engine.create_task,learner=req.learner,request=req.task,material=req.material,
                mode=req.mode,image_url=req.image_url,request_id=req.request_id,assignment_id=req.assignment_id,followup_id=req.followup_id)


@app.get('/api/sessions/{sid}')
def get_session(sid:str): return call(engine.session,sid)


@app.post('/api/sessions/{sid}/{action}')
def act(sid:str,action:str,req:Operation):
    if action not in ('attempt','hint','image','confirm','reply','explain','check','resume_practice','confirm_task'): raise HTTPException(404)
    body={'image_url':req.image_url} if action=='image' else {} if action in ('hint','explain','check','resume_practice') else {'text':req.text}
    return call(engine.operation,sid,req.request_id,action,body,req.version)


@app.get('/api/teacher/sessions/{sid}/trace')
def traces(sid: str): return call(engine.traces, sid)


@app.get('/api/teacher/sessions/{sid}/replay')
def replay(sid: str): return call(engine.replay, sid)


@app.get('/api/progress/{learner}')
def progress(learner:str): return call(engine.progress,learner)


@app.get('/api/teacher/report')
def report(learner:str|None=None): return call(engine.report,learner)


@app.get('/api/teacher/images/{event_id}')
def original_image(event_id:int):
    with engine.connect() as c:
        row=c.execute("SELECT payload FROM events WHERE id=? AND type='image'",(event_id,)).fetchone()
    if not row: raise HTTPException(404)
    return json.loads(row['payload'])


@app.post('/api/teacher/attempts/{event_id}/corrections')
def correct(event_id:int,req:Correction):
    return call(engine.correct,event_id,req.status,req.reason,req.request_id)


def question(s):
    if s.get('engine') == 'open-task-v1':
        if s['needs_review']: return '教师已修订证据，请新建任务重新检查。'
        if not s['task']: return '请确认或修正题目图片的识别结果。'
        t=s['task']
        pages=t.get('source_pages') or []
        where=f"\n\n材料出处：第{'、'.join(map(str,pages))}页" if pages else ''
        return f"### {'独立检查' if t['phase']=='check' else '当前任务'} · {t['domain']}\n\n{t['question']}\n\n学习目标：{t['objective']}{where}"
    if s['needs_review']: return '教师已修正记录，请新建该知识点会话重新检查。'
    if s['done']: return '本次学习已结束，可以在学习记录中查看证据或选择新练习。'
    label='独立检查 · 请独立作答' if s['item']['phase']=='check' else '教学练习'
    pending = s.get('pending_question')
    if pending: label += ' · 请回答追问，或提交修正后的数学步骤'
    return f"### {label}\n\n{s['item']['prompt']}\n\n课程依据：`{s['item']['source']}`"


def history(s):
    text=[]
    for e in s.get('history',[]):
        if e.get('text'): text.append(f"作答 #{e['id']}：\n{e['text']}")
        if e.get('feedback'): text.append('反馈：'+e['feedback'])
        if e.get('criteria_results'):
            labels={'correct':'通过','incorrect':'错误','incomplete':'未完成','unknown':'待核对'}
            text.extend('评分项：'+x['description']+' — '+labels[x['status']] for x in e['criteria_results'])
        if e.get('hint'): text.append(f"第{e['level']}级提示：{e['hint']}")
    return '\n\n'.join(text) or '还没有作答记录。'


def start(learner,skill):
    try:
        s=engine.create(learner,skill)
        return s['session_id'],s,question(s),'请写出你的解题步骤。',history(s),''
    except Exception as e: raise gr.Error(str(e))


def resume(sid):
    try:
        s=engine.session(sid)
        pending=s.get('pending_image')
        return sid,s,question(s),'会话已恢复。',history(s),pending['text'] if pending else ''
    except Exception as e: raise gr.Error(str(e))


def operate(action,s,text='',image=None,request_id=None):
    if not s: raise gr.Error('请先开始或恢复会话')
    if action == 'confirm' and s.get('pending_image',{}):
        if s['pending_image'].get('purpose') == 'task': action = 'confirm_task'
    body={}
    if action in ('attempt','confirm','reply','confirm_task'): body={'text':text}
    if action=='image':
        if not image: raise gr.Error('请先选择图片')
        try: body={'image_url':image_data(image)}
        except ValueError as e: raise gr.Error(str(e))
    try:
        # Same UI submission/state/body has the same key, including after a retry.
        import hashlib
        key=request_id or hashlib.sha256(json.dumps([s['session_id'],s['version'],action,body],sort_keys=True).encode()).hexdigest()
        result=engine.operation(s['session_id'],key,action,body,s['version'])
        new=engine.session(s['session_id'])
        feedback=result.get('feedback') or f"第{result['level']}级提示：{result['hint']}"
        transcript=new['pending_image']['text'] if new['pending_image'] else ''
        return new,question(new),feedback,history(new),transcript
    except (ValueError,ModelUnavailable) as e: raise gr.Error(str(e))


def teacher(learner):
    return engine.report(learner.strip() or None)


def revision(event_id,status,reason):
    try:
        engine.correct(int(event_id),status,reason,uuid.uuid4().hex)
        return '修正已记录。原始作答保留，相关会话需重新检查。',engine.report()
    except ValueError as e: raise gr.Error(str(e))


def image_data(path):
    if not path: return ''
    path=Path(path)
    if path.stat().st_size > 30*1024**2: raise ValueError('图片需小于30MB')
    return file_to_data_url(path)


def start_task(learner,task,material,mode,task_image,material_file,assignment_id="",followup_id=""):
    try:
        if material_file:
            uploaded=read_material(material_file)
            material = uploaded if not material.strip() else material+'\n'+uploaded
        s=engine.create_task(learner=learner,request=task,material=material,mode=mode,image_url=image_data(task_image),assignment_id=assignment_id.strip() or None,followup_id=followup_id.strip() or None)
        pending=s.get('pending_image')
        return s['session_id'],s,question(s),'请确认图片题目。' if pending else '请提交你的尝试，也可以请求提示或讲解。',history(s),pending['text'] if pending else ''
    except Exception as e: raise gr.Error(str(e))


@app.post('/api/teacher/tasks/{sid}/attempts/{event_id}/corrections')
def correct_open(sid:str,event_id:int,req:Correction):
    return call(engine.open.correct,sid,event_id,req.status,req.reason,req.request_id)


@app.get('/api/teacher/tasks/{sid}/images/{event_id}')
def open_image(sid:str,event_id:int):
    payload=engine.open.image_event(sid,event_id)
    if not payload: raise HTTPException(404)
    return payload


@app.post('/api/teacher/assignments')
def publish_assignment(req:Assignment):
    return call(engine.open.publish_assignment,req.model_dump())


@app.get('/api/teacher/dashboard')
def teaching_dashboard(class_id:str|None=None):
    return call(engine.open.dashboard,class_id)


@app.get('/api/teacher/memory/{learner}')
def teaching_memory(learner:str,class_id:str|None=None):
    return call(engine.open.learning_memory,learner,class_id)


class FollowupRequest(FollowupBatch):
    request_id: str = Field(min_length=1,max_length=100)


class FollowupRevision(BaseModel):
    action: Literal['postpone','cancel']
    due_at: float | None = Field(default=None,ge=0,allow_inf_nan=False)
    reason: str = Field(min_length=1,max_length=400)
    request_id: str = Field(min_length=1,max_length=100)


class ReviewConfirmation(BaseModel):
    reason: str = Field(min_length=1,max_length=400)
    request_id: str = Field(min_length=1,max_length=100)


@app.post('/api/teacher/tasks/{sid}/attempts/{event_id}/reviews')
def confirm_review(sid:str,event_id:int,req:ReviewConfirmation):
    return call(engine.open.confirm_review,sid,event_id,req.reason,req.request_id)


@app.post('/api/teacher/paths')
def publish_path(req:Curriculum):
    return call(engine.open.publish_path,req.model_dump())


@app.get('/api/learning/next')
def next_learning(path_id:str,learner:str):
    return call(engine.open.next_step,path_id,learner)


@app.post('/api/teacher/followups')
def assign_followups(req:FollowupRequest):
    return call(engine.open.assign_followups,req.model_dump(exclude={'request_id'}),req.request_id)


@app.get('/api/learning/followups')
def learning_followups(class_id:str,learner:str):
    return call(engine.open.followups,class_id,learner)


@app.patch('/api/teacher/followups/{key}')
def revise_followup(key:str,req:FollowupRevision):
    return call(engine.open.revise_followup,key,req.action,req.due_at,req.reason,req.request_id)


class PackSave(Pack):
    request_id: str = Field(min_length=1,max_length=100)
    version: int | None = Field(default=None,ge=1)


class PackGenerate(GeneratePack):
    request_id: str = Field(min_length=1,max_length=100)


class PackPublish(Distribution):
    request_id: str = Field(min_length=1,max_length=100)
    version: int = Field(ge=1)


@app.post('/api/teacher/packs')
def save_pack(req:PackSave):
    return call(engine.open.save_pack,req.model_dump(exclude={'request_id','version'}),req.request_id)


@app.post('/api/teacher/packs/generate')
def generate_pack(req:PackGenerate):
    return call(engine.open.generate_pack,req.model_dump(exclude={'request_id'}),req.request_id)


@app.get('/api/teacher/packs')
def list_packs(class_id:str):
    return call(engine.open.packs,class_id)


@app.get('/api/teacher/packs/{pack_id}')
def get_pack(pack_id:str):
    return call(engine.open.load_pack,pack_id)


@app.put('/api/teacher/packs/{pack_id}')
def update_pack(pack_id:str,req:PackSave):
    return call(engine.open.save_pack,req.model_dump(exclude={'request_id','version'}),req.request_id,pack_id,req.version)


@app.post('/api/teacher/packs/{pack_id}/publish')
def publish_pack(pack_id:str,req:PackPublish):
    return call(engine.open.publish_pack,pack_id,req.version,req.model_dump(exclude={'request_id','version'}),req.request_id)


@app.get('/api/learning/inbox')
def learning_inbox(class_id:str,learner:str):
    return call(engine.open.inbox,class_id,learner)


def inbox_options(cls,learner):
    items=engine.open.inbox(cls.strip(),learner.strip())
    choices=[(f"{x['course_id']} · {x['objective']} · {x['status']}",x['followup_id'])
             for x in items if x['status'] not in ('cancelled','needs_recheck','completed')]
    return gr.Dropdown(choices=choices,value=choices[0][1] if choices else None)


def start_inbox(learner,followup):
    if not followup:raise gr.Error('请先选择任务')
    return start_task(learner,'','','problem',None,None,'',followup)


PACK_COLUMNS=['课程','知识点','学科','教学类型','学习目标','题目','评分要求','参考答案','独立检查要求（每行一项）']
PACK_FIELDS=['course_id','concept_id','domain','task_type','objective','question','rubric','reference','check_requirements']


def pack_display(pack):
    rows=[]
    for item in pack['body']['items']:
        rows.append(["\n".join(item.get(k,[])) if k=='check_requirements' else item.get(k,'') for k in PACK_FIELDS])
    criteria=[[str(i+1),c['id'],c['kind'],c['description'],c.get('expected',''),c.get('concept_id','')]
              for i,item in enumerate(pack['body']['items']) for c in item.get('criteria',[])]
    return pack,pack['pack_id'],rows,f"{pack['body']['title']} · {pack['status']} · 版本 {pack['version']}",criteria,False


def generate_pack_ui(cls,title,material,goals):
    try:
        values=[dict(zip(['course_id','concept_id','domain','task_type','objective'],r)) for r in goals if any(r)]
        for value in values:value['task_type']=value.get('task_type') or 'open'
        return pack_display(engine.open.generate_pack(dict(class_id=cls,title=title,material=material,goals=values),uuid.uuid4().hex))
    except (ValueError,ModelUnavailable,Conflict) as e:raise gr.Error(str(e))


def edited_pack_body(pack,rows,criteria_rows=None):
    if not pack:raise gr.Error('请先生成或加载备课包')
    if len(rows)!=len(pack['body']['items']):raise gr.Error('请保持行数；增加任务请新建备课包')
    from copy import deepcopy
    body=deepcopy(pack['body'])
    for item,row in zip(body['items'],rows):
        for key,value in zip(PACK_FIELDS,row):
            item[key]=[v.strip() for v in str(value or '').splitlines() if v.strip()] if key=='check_requirements' else str(value or '')
    if criteria_rows is not None:
        for item in body['items']:item['criteria']=[]
        for row in criteria_rows:
            if not any(row):continue
            try:
                position=int(row[0])-1
                if not 0<=position<len(body['items']):raise ValueError('题号不存在')
                body['items'][position]['criteria'].append(dict(zip(['id','kind','description','expected','concept_id'],row[1:])))
            except (ValueError,TypeError,IndexError) as e:raise gr.Error('分项标准请填写有效题号：'+str(e))
    return body


def save_pack_ui(pack,rows,criteria_rows=None):
    body=edited_pack_body(pack,rows,criteria_rows)
    try:return pack_display(engine.open.save_pack(body,uuid.uuid4().hex,pack['pack_id'],pack['version']))
    except (ValueError,Conflict) as e:raise gr.Error(str(e))


def publish_pack_ui(pack,rows,criteria_rows,learners,reviewed,note):
    if not pack:raise gr.Error('请先生成或加载备课包')
    try:
        Distribution(learners=[x.strip() for x in learners.splitlines() if x.strip()],reviewed=reviewed,review_note=note)
    except ValueError as e:raise gr.Error(str(e))
    body=edited_pack_body(pack,rows,criteria_rows)
    try:
        result=engine.open.publish_pack(pack['pack_id'],pack['version'],dict(
            learners=[x.strip() for x in learners.splitlines() if x.strip()],
            reviewed=reviewed,review_note=note,due_at=0),uuid.uuid4().hex,edited_body=body)
        return pack_display(result)
    except (ValueError,Conflict) as e:raise gr.Error(str(e))


def next_text(path_id,learner):
    try:
        result=engine.open.next_step(path_id.strip(),learner.strip())
        labels={'resume':'继续未完成作答','followup':'完成教师跟进任务','request_review':'请教师安排复习',
                'check':'进行独立检查','remediate':'补救学习','diagnose':'先做诊断','complete':'本轮路径完成'}
        lines=[f"### {labels[result['action']]}",result['reason']]
        for key,label in [('session_id','会话编号'),('followup_id','跟进任务编号'),('assignment_id','教师任务编号'),('concept_id','知识点')]:
            if result.get(key):lines.append(f"{label}：`{result[key]}`")
        return '\n\n'.join(lines)
    except ValueError as e:raise gr.Error(str(e))


def followups_text(class_id,learner):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    rows=engine.open.followups(class_id.strip(),learner.strip())
    labels={'queued':'待开始','active':'进行中','completed':'独立检查通过','cancelled':'已撤销','needs_recheck':'证据失效，需重查'}
    return '\n\n'.join(f"- `{r['id']}` · {r['body']['objective']} · {labels[r['status']]} · {datetime.fromtimestamp(r['due_at'],ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')}（北京时间）" for r in rows) or '暂无跟进任务。'


def dashboard_text(class_id):
    report=engine.open.dashboard(class_id.strip() or None)
    labels={'insufficient_evidence':'证据不足','needs_recheck':'需重新检查','needs_support':'需要帮助',
            'independent_observed':'观察到独立完成','assisted_complete':'辅助完成','practice_complete':'练习完成'}
    lines=['### 学情与下一步']
    for row in report['learning']:
        lines.append(f"- **{row['learner']}** · {row['objective']}：{labels[row['state']]}；{row['next_step']}。独立检查记录 {row['independent_count']} 次。")
    if not report['learning']: lines.append('暂无此班级的学习证据。')
    lines.append('### 建议分组补救')
    for group in report.get('support_groups',[]):
        lines.append(f"- {group['objective']} · {group.get('criterion','')}：{'、'.join(group['learners'])}")
    lines.append('### 待复核作答（优先风险项，其余抽样）')
    for row in report['review_queue']:
        lines.append(f"- {row['learner']} · 作答 #{row['event_id']} · 会话 `{row['session_id']}`：{row['text']}（{row['status'] or '反馈待审'}；{row.get('priority','')}）")
    return '\n\n'.join(lines)


with gr.Blocks(title='OmniEdu · 教学工作台',theme=gr.themes.Soft()) as demo:
    gr.Markdown('# OmniEdu · 教学工作台\n输入题目、上传图片，或用自己的学习材料开始。')
    gr.Markdown('本机内测：学生和教师为演示视图，共享本地数据。')
    with gr.Tab('学生练习'):
        session_state=gr.State(None)
        learner=gr.Textbox(label='学生名称',value='学生A')
        task_input=gr.Textbox(label='题目或学习目标',lines=3,placeholder='粘贴任何学科的题目，或说明你想学习什么')
        mode=gr.Radio(label='开始方式',choices=[('辅导这道题','problem'),('根据目标或材料学习','learn')],value='problem')
        with gr.Accordion('题目图片与学习材料',open=False):
            task_image=gr.Image(type='filepath',label='题目图片 · 识别后需要你确认')
            material=gr.Textbox(label='参考材料',lines=5)
            material_file=gr.File(label='上传材料（UTF-8 文本或文字版 PDF）',file_types=['.txt','.md','.pdf'],type='filepath')
        assignment_id=gr.Textbox(label='教师任务编号（可选；填写后使用教师题目与评分规则）')
        followup_id=gr.Textbox(label='跟进任务编号（可选；填写后优先打开该复习/补救任务）')
        begin_task=gr.Button('创建教学任务',variant='primary')
        with gr.Accordion('我的班级任务',open=True):
            inbox_class=gr.Textbox(label='班级')
            inbox_refresh=gr.Button('加载我的任务')
            inbox_choice=gr.Dropdown(label='选择课程任务',choices=[])
            inbox_begin=gr.Button('开始所选任务')
            inbox_refresh.click(inbox_options,[inbox_class,learner],[inbox_choice])
        with gr.Accordion('课程路径与我的跟进任务',open=False):
            student_class=gr.Textbox(label='班级编号')
            list_followups=gr.Button('查看我的复习与补救任务')
            followup_list=gr.Markdown()
            list_followups.click(followups_text,[student_class,learner],[followup_list])
            path_input=gr.Textbox(label='课程路径编号')
            next_button=gr.Button('查看下一步学习安排')
            next_view=gr.Markdown()
            next_button.click(next_text,[path_input,learner],[next_view])
        with gr.Accordion('示例课程（可选）',open=False):
            skill=gr.Dropdown(label='代数示例',choices=[(x['name'],x['id']) for x in SKILLS.values()],value='k01')
            begin=gr.Button('打开示例')
        with gr.Row():
            sid=gr.Textbox(label='会话编号 · 保存后可恢复')
            restore=gr.Button('恢复会话')
        q=gr.Markdown('先输入题目、学习目标或材料。')
        work=gr.Textbox(label='你的作答或对老师追问的回答',lines=5,placeholder='写出你的尝试、理由，或告诉老师哪里不理解')
        with gr.Row():
            submit=gr.Button('提交作答',variant='primary')
            hint=gr.Button('需要更多提示')
            reply=gr.Button('回答老师追问')
        with gr.Row():
            explain=gr.Button('请完整讲解')
            new_check=gr.Button('开始新的独立检查')
            back=gr.Button('回到原任务继续学习')
        feedback=gr.Textbox(label='本次反馈',interactive=False,lines=3)
        with gr.Accordion('图片转写确认 / 上传作答图片',open=True):
            image=gr.Image(type='filepath',label='PNG / JPEG / WebP')
            transcribe=gr.Button('识别图片')
            transcript=gr.Textbox(label='确认或修正转写 · 题目图片保留题目，作答图片保留作答',lines=5)
            confirm=gr.Button('确认转写并提交判定')
        hist=gr.Textbox(label='本次学习记录',interactive=False,lines=10)
        show_progress=gr.Button('查看历史进度与待复习知识点')
        progress_json=gr.JSON(label='学习证据与可恢复会话')
        inbox_begin.click(start_inbox,[learner,inbox_choice],[sid,session_state,q,feedback,hist,transcript])
        begin_task.click(start_task,[learner,task_input,material,mode,task_image,material_file,assignment_id,followup_id],[sid,session_state,q,feedback,hist,transcript])
        begin.click(start,[learner,skill],[sid,session_state,q,feedback,hist,transcript])
        restore.click(resume,[sid],[sid,session_state,q,feedback,hist,transcript])
        outs=[session_state,q,feedback,hist,transcript]
        submit.click(lambda s,t:operate('attempt',s,t),[session_state,work],outs)
        reply.click(lambda s,t:operate('reply',s,t),[session_state,work],outs)
        explain.click(lambda s:operate('explain',s),[session_state],outs)
        new_check.click(lambda s:operate('check',s),[session_state],outs)
        back.click(lambda s:operate('resume_practice',s),[session_state],outs)
        hint.click(lambda s:operate('hint',s),[session_state],outs)
        transcribe.click(lambda s,i:operate('image',s,image=i),[session_state,image],outs)
        confirm.click(lambda s,t:operate('confirm',s,t),[session_state,transcript],outs)
        show_progress.click(engine.progress,[learner],[progress_json])
    with gr.Tab('批量备课与发布'):
        gr.Markdown('按多个课程或学科列出学习目标，生成后逐题检查并编辑。确认发布后，学生可以直接从“我的班级任务”进入。')
        pack_state=gr.State(None)
        pack_class=gr.Textbox(label='班级')
        pack_title=gr.Textbox(label='备课包名称')
        pack_material=gr.Textbox(label='本批任务参考材料',lines=6)
        gr.Markdown('教学类型：'+ '；'.join(k+'（'+v['label']+'）' for k,v in PROFILES.items()))
        goals=gr.Dataframe(headers=['课程','知识点','学科','教学类型','学习目标'],datatype=['str']*5,type='array',row_count=3,col_count=(5,'fixed'))
        pack_generate=gr.Button('生成待审核草稿（每批最多6个目标）')
        pack_key=gr.Textbox(label='备课包编号（可恢复编辑）')
        pack_load=gr.Button('加载备课包')
        pack_rows=gr.Dataframe(headers=PACK_COLUMNS,datatype=['str']*len(PACK_COLUMNS),type='array',row_count=1,col_count=(len(PACK_COLUMNS),'fixed'),wrap=True)
        pack_criteria=gr.Dataframe(label='分项标准（题号从1开始；每题最多4项）',headers=['题号','编号','类型','评分要求','预期值','知识点'],datatype=['str']*6,type='array',row_count=1,col_count=(6,'fixed'),wrap=True)
        pack_message=gr.Textbox(label='备课状态',interactive=False)
        pack_save=gr.Button('保存编辑')
        pack_learners=gr.Textbox(label='分发给学生（每行一个姓名；留空只发布到题库）',lines=4)
        pack_review=gr.Checkbox(label='已逐题核对题目、参考答案、评分与独立检查要求',value=False)
        pack_note=gr.Textbox(label='审核说明')
        pack_publish=gr.Button('确认发布并分发')
        pack_outputs=[pack_state,pack_key,pack_rows,pack_message,pack_criteria,pack_review]
        pack_generate.click(generate_pack_ui,[pack_class,pack_title,pack_material,goals],pack_outputs)
        pack_load.click(lambda key:pack_display(engine.open.load_pack(key)),[pack_key],pack_outputs)
        pack_save.click(save_pack_ui,[pack_state,pack_rows,pack_criteria],pack_outputs)
        pack_publish.click(publish_pack_ui,[pack_state,pack_rows,pack_criteria,pack_learners,pack_review,pack_note],pack_outputs)
    with gr.Tab('教师备课与跟进'):
        class_filter=gr.Textbox(label='班级编号（留空查看本机全部）')
        dashboard_button=gr.Button('查看学情与待复核作答')
        dashboard_view=gr.Markdown()
        dashboard_button.click(dashboard_text,[class_filter],[dashboard_view])
        gr.Markdown('发布后的任务版本保持不变；调整题目或规则时发布新版本。同一知识点使用相同课程和知识点编号，跨题接续学习记录。')
        with gr.Row():
            cls=gr.Textbox(label='班级编号')
            course=gr.Textbox(label='课程编号')
            concept=gr.Textbox(label='知识点编号')
        subject=gr.Textbox(label='学科')
        objective=gr.Textbox(label='可观察的学习目标')
        task_type=gr.Dropdown(label='教学类型',choices=[(v['label'],k) for k,v in PROFILES.items()],value='open')
        check_requirements=gr.Textbox(label='独立检查要求（每行一项，使用可迁移要求，避免填写本题答案）',lines=3)
        tq=gr.Textbox(label='题目',lines=3)
        rubric=gr.Textbox(label='评分要求（注明解释、单位、形式等要求）',lines=3)
        reference=gr.Textbox(label='教师参考答案',lines=3)
        tm=gr.Textbox(label='教学材料',lines=4)
        prerequisite=gr.Textbox(label='先修知识')
        check_bank=gr.Textbox(label='教师审核检查题编号（可选，每行一个；留空则生成并审核新题）',lines=2)
        guidance=gr.Textbox(label='教学指导（避免在这里填写检查题答案）')
        allow_explanation=gr.Checkbox(label='允许学生请求完整讲解',value=True)
        with gr.Accordion('分项评分与验证器（可选，最多4项）',open=False):
            gr.Markdown('类型：algebra 代数、numeric 数值、unit 指定单位、explanation 解释、factorization 因式分解、model 开放评分。数值/单位/因式分解需填写预期值；学生只看到要求，不显示预期值。')
            criteria_table=gr.Dataframe(headers=['编号','类型','评分要求','预期值','关联知识点'],datatype=['str']*5,type='array',row_count=4,col_count=(5,'fixed'))
            initial=gr.Textbox(label='验证器原式（可留空自动识别单个一元方程）')
            initial_kind=gr.Radio(choices=['none','equation','expression'],value='none',label='验证器类型')
            preview_button=gr.Button('核对验证器绑定')
            preview=gr.JSON(label='题目验证器绑定结果')
            def preview_binding(question,initial,kind):
                from .criteria import bind_algebra
                try:return bind_algebra(question,initial,kind)
                except ValueError as e:raise gr.Error(str(e))
            preview_button.click(preview_binding,[tq,initial,initial_kind],[preview])
        publish=gr.Button('发布教师任务')
        published_id=gr.Textbox(label='任务编号 · 交给学生填写',interactive=False)
        def publish_ui(cls,course,concept,subject,objective,tq,rubric,reference,tm,prerequisite,guidance,allow,criteria_rows,initial,kind,task_type,requirements,check_bank):
            try:
                return engine.open.publish_assignment(dict(class_id=cls,course_id=course,concept_id=concept,
                    domain=subject,objective=objective,question=tq,rubric=rubric,reference=reference,material=tm,
                    task_type=task_type,check_requirements=[x.strip() for x in requirements.splitlines() if x.strip()],
                    check_assignment_ids=[x.strip() for x in check_bank.splitlines() if x.strip()],
                    prerequisite=prerequisite,teaching_note=guidance,allow_explanation=allow,algebra_initial=initial,algebra_kind=kind,
                    criteria=[dict(id=r[0],kind=r[1] or 'model',description=r[2],expected=r[3] or '',concept_id=r[4] or '') for r in (criteria_rows or []) if r[0]]))['assignment_id']
            except ValueError as e: raise gr.Error(str(e))
        publish.click(publish_ui,[cls,course,concept,subject,objective,tq,rubric,reference,tm,prerequisite,guidance,allow_explanation,criteria_table,initial,initial_kind,task_type,check_requirements,check_bank],[published_id])
    with gr.Tab('课程路径与补救安排'):
        plan_class=gr.Textbox(label='班级编号')
        plan_course=gr.Textbox(label='课程编号')
        plan_title=gr.Textbox(label='课程路径名称')
        plan_rows=gr.Dataframe(headers=['知识点编号','名称','已发布教师任务编号','先修知识点（逗号分隔）'],datatype=['str']*4,type='array',row_count=3,col_count=(4,'fixed'))
        plan_publish=gr.Button('发布课程路径')
        plan_result=gr.Textbox(label='课程路径编号',interactive=False)
        def publish_path_ui(cls,course,title,rows):
            try:
                return engine.open.publish_path(dict(class_id=cls,course_id=course,title=title,
                    nodes=[dict(concept_id=r[0],title=r[1],assignment_id=r[2],prerequisite_ids=[x.strip() for x in (r[3] or '').replace('，',',').split(',') if x.strip()]) for r in rows if r[0]]))['path_id']
            except ValueError as e:raise gr.Error(str(e))
        plan_publish.click(publish_path_ui,[plan_class,plan_course,plan_title,plan_rows],[plan_result])
        gr.Markdown('复习和诊断使用未见的新题，完成后记录独立证据；补救任务允许提示，但仍须通过后续独立检查才算完成。')
        group_names=gr.Textbox(label='学生名单（每行一人）',lines=3)
        followup_assignment=gr.Textbox(label='已发布的教师任务编号')
        followup_kind=gr.Radio(choices=[('复习','review'),('补救','remediation'),('诊断','diagnostic')],value='remediation',label='跟进类型')
        due_hours=gr.Number(label='多少小时后到期（0表示现在）',value=24,minimum=0,maximum=8760)
        followup_reason=gr.Textbox(label='安排依据')
        assign_button=gr.Button('为名单中的学生布置任务')
        assigned_results=gr.JSON(label='已布置任务与编号')
        def assign_ui(cls,aid,names,kind,hours,reason):
            import time
            try:return engine.open.assign_followups(dict(class_id=cls,assignment_id=aid,
                learners=[x.strip() for x in names.splitlines() if x.strip()],kind=kind,due_at=time.time()+float(hours)*3600,reason=reason),uuid.uuid4().hex)
            except (ValueError,Conflict) as e:raise gr.Error(str(e))
        assign_button.click(assign_ui,[plan_class,followup_assignment,group_names,followup_kind,due_hours,followup_reason],[assigned_results])
        revise_id=gr.Textbox(label='需要调整的跟进任务编号')
        revise_kind=gr.Radio(choices=[('延期','postpone'),('撤销','cancel')],value='postpone',label='调整方式')
        revise_button=gr.Button('记录调整（使用上方小时数与安排依据）')
        def revise_ui(key,kind,hours,reason):
            import time
            try:return engine.open.revise_followup(key,kind,time.time()+float(hours)*3600,reason,uuid.uuid4().hex)
            except (ValueError,Conflict) as e:raise gr.Error(str(e))
        revise_button.click(revise_ui,[revise_id,revise_kind,due_hours,followup_reason],[assigned_results])
    with gr.Tab('教师证据'):
        filter_learner=gr.Textbox(label='学生筛选 · 留空查看全部')
        refresh=gr.Button('查看学情与原始作答')
        teacher_json=gr.JSON(label='知识点错误、提示使用、独立检查与证据编号')
        refresh.click(teacher,[filter_learner],[teacher_json])
        with gr.Row():
            event_id=gr.Number(label='需要修正的作答编号',precision=0)
            corrected=gr.Dropdown(label='修正判定',choices=['correct','incorrect','incomplete','unknown'],value='incorrect')
        reason=gr.Textbox(label='修正依据',lines=2)
        fix=gr.Button('记录人工修正')
        revision_msg=gr.Textbox(label='修正结果',interactive=False)
        fix.click(revision,[event_id,corrected,reason],[revision_msg,teacher_json])
        open_sid=gr.Textbox(label='自定义任务会话编号（open_开头）')
        open_fix=gr.Button('修订自定义任务作答')
        confirm_review_button=gr.Button('确认原判定正确（保留有效学习证据）')
        def confirm_review_ui(session,event,reason):
            try:
                engine.open.confirm_review(session,int(event),reason,uuid.uuid4().hex)
                return '已记录教师复核，原证据保持有效。',engine.report()
            except (ValueError,Conflict) as e:raise gr.Error(str(e))
        confirm_review_button.click(confirm_review_ui,[open_sid,event_id,reason],[revision_msg,teacher_json])
        def revise_open(session,event,status,reason):
            try:
                engine.open.correct(session,int(event),status,reason,uuid.uuid4().hex)
                return '修订已保存，需重新检查。',engine.report()
            except ValueError as e: raise gr.Error(str(e))
        open_fix.click(revise_open,[open_sid,event_id,corrected,reason],[revision_msg,teacher_json])
        trace_sid=gr.Textbox(label='教学会话编号')
        trace_button=gr.Button('查看教学决策与证据')
        replay_button=gr.Button('回放策略决策（不调用模型）')
        trace_json=gr.JSON(label='教学执行轨迹')
        trace_button.click(engine.traces,[trace_sid],[trace_json])
        replay_button.click(engine.replay,[trace_sid],[trace_json])
        image_id=gr.Number(label='原始图片事件编号（image_event_id）',precision=0)
        view_image=gr.Button('查看对应原始图片')
        original=gr.Image(label='原始作业',interactive=False)
        def show_image(i,session):
            import io
            from PIL import Image
            payload=open_image(session,int(i)) if session.startswith('open_') else original_image(int(i))
            return Image.open(io.BytesIO(base64.b64decode(payload['image_url'].split(',',1)[1])))
        view_image.click(show_image,[image_id,open_sid],[original])

demo.queue(default_concurrency_limit=4,max_size=16)
app=gr.mount_gradio_app(app,demo,path='/',show_error=True)
