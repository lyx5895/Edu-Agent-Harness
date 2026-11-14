"""HTTP request contracts and routes, independent of the Gradio interface."""
import json
from types import SimpleNamespace
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from harness.legacy.course import SKILLS
from harness.errors import Conflict
from harness.llm.model import ModelUnavailable
from harness.teaching.teacher import Assignment
from harness.teaching.planning import Curriculum, FollowupBatch
from harness.teaching.preparation import Pack, GeneratePack, Distribution



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


class PackSave(Pack):
    request_id: str = Field(min_length=1,max_length=100)
    version: int | None = Field(default=None,ge=1)


class PackGenerate(GeneratePack):
    request_id: str = Field(min_length=1,max_length=100)


class PackPublish(Distribution):
    request_id: str = Field(min_length=1,max_length=100)
    version: int = Field(ge=1)


def call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except ModelUnavailable as e:
        raise HTTPException(503,str(e))
    except Conflict as e:
        raise HTTPException(409,str(e))
    except ValueError as e:
        raise HTTPException(400,str(e))


def create_router(get_engine):
    """Resolve the gateway per call so each app can own an isolated runtime."""
    router = APIRouter()

    @router.get('/health')
    def health(): return {'ready':True,'mode':'local-demo','model_base_url':get_engine().model.base_url}

    @router.get('/api/skills')
    def skills(): return list(SKILLS.values())

    @router.post('/api/sessions')
    def create_session(req:NewSession):
        if req.skill:
            if req.task or req.material or req.image_url: raise HTTPException(400,'示例课程与自定义任务请分开创建')
            return call(get_engine().create,req.learner,req.skill)
        return call(get_engine().create_task,learner=req.learner,request=req.task,material=req.material,
                    mode=req.mode,image_url=req.image_url,request_id=req.request_id,assignment_id=req.assignment_id,followup_id=req.followup_id)

    @router.get('/api/sessions/{sid}')
    def get_session(sid:str): return call(get_engine().session,sid)

    @router.post('/api/sessions/{sid}/{action}')
    def act(sid:str,action:str,req:Operation):
        if action not in ('attempt','hint','image','confirm','reply','explain','check','resume_practice','confirm_task'): raise HTTPException(404)
        body={'image_url':req.image_url} if action=='image' else {} if action in ('hint','explain','check','resume_practice') else {'text':req.text}
        return call(get_engine().operation,sid,req.request_id,action,body,req.version)

    @router.get('/api/teacher/sessions/{sid}/trace')
    def traces(sid: str): return call(get_engine().traces, sid)

    @router.get('/api/teacher/sessions/{sid}/replay')
    def replay(sid: str): return call(get_engine().replay, sid)

    @router.get('/api/progress/{learner}')
    def progress(learner:str): return call(get_engine().progress,learner)

    @router.get('/api/teacher/report')
    def report(learner:str|None=None): return call(get_engine().report,learner)

    @router.get('/api/teacher/images/{event_id}')
    def original_image(event_id:int):
        with get_engine().connect() as c:
            row=c.execute("SELECT payload FROM events WHERE id=? AND type='image'",(event_id,)).fetchone()
        if not row: raise HTTPException(404)
        return json.loads(row['payload'])

    @router.post('/api/teacher/attempts/{event_id}/corrections')
    def correct(event_id:int,req:Correction):
        return call(get_engine().correct,event_id,req.status,req.reason,req.request_id)

    @router.post('/api/teacher/tasks/{sid}/attempts/{event_id}/corrections')
    def correct_open(sid:str,event_id:int,req:Correction):
        return call(get_engine().open.correct,sid,event_id,req.status,req.reason,req.request_id)

    @router.get('/api/teacher/tasks/{sid}/images/{event_id}')
    def open_image(sid:str,event_id:int):
        payload=get_engine().open.image_event(sid,event_id)
        if not payload: raise HTTPException(404)
        return payload

    @router.post('/api/teacher/assignments')
    def publish_assignment(req:Assignment):
        return call(get_engine().open.publish_assignment,req.model_dump())

    @router.get('/api/teacher/dashboard')
    def teaching_dashboard(class_id:str|None=None):
        return call(get_engine().open.dashboard,class_id)

    @router.get('/api/teacher/memory/{learner}')
    def teaching_memory(learner:str,class_id:str|None=None):
        return call(get_engine().open.learning_memory,learner,class_id)

    @router.post('/api/teacher/tasks/{sid}/attempts/{event_id}/reviews')
    def confirm_review(sid:str,event_id:int,req:ReviewConfirmation):
        return call(get_engine().open.confirm_review,sid,event_id,req.reason,req.request_id)

    @router.post('/api/teacher/paths')
    def publish_path(req:Curriculum):
        return call(get_engine().open.publish_path,req.model_dump())

    @router.get('/api/learning/next')
    def next_learning(path_id:str,learner:str):
        return call(get_engine().open.next_step,path_id,learner)

    @router.post('/api/teacher/followups')
    def assign_followups(req:FollowupRequest):
        return call(get_engine().open.assign_followups,req.model_dump(exclude={'request_id'}),req.request_id)

    @router.get('/api/learning/followups')
    def learning_followups(class_id:str,learner:str):
        return call(get_engine().open.followups,class_id,learner)

    @router.patch('/api/teacher/followups/{key}')
    def revise_followup(key:str,req:FollowupRevision):
        return call(get_engine().open.revise_followup,key,req.action,req.due_at,req.reason,req.request_id)

    @router.post('/api/teacher/packs')
    def save_pack(req:PackSave):
        return call(get_engine().open.save_pack,req.model_dump(exclude={'request_id','version'}),req.request_id)

    @router.post('/api/teacher/packs/generate')
    def generate_pack(req:PackGenerate):
        return call(get_engine().open.generate_pack,req.model_dump(exclude={'request_id'}),req.request_id)

    @router.get('/api/teacher/packs')
    def list_packs(class_id:str):
        return call(get_engine().open.packs,class_id)

    @router.get('/api/teacher/packs/{pack_id}')
    def get_pack(pack_id:str):
        return call(get_engine().open.load_pack,pack_id)

    @router.put('/api/teacher/packs/{pack_id}')
    def update_pack(pack_id:str,req:PackSave):
        return call(get_engine().open.save_pack,req.model_dump(exclude={'request_id','version'}),req.request_id,pack_id,req.version)

    @router.post('/api/teacher/packs/{pack_id}/publish')
    def publish_pack(pack_id:str,req:PackPublish):
        return call(get_engine().open.publish_pack,pack_id,req.version,req.model_dump(exclude={'request_id','version'}),req.request_id)

    @router.get('/api/learning/inbox')
    def learning_inbox(class_id:str,learner:str):
        return call(get_engine().open.inbox,class_id,learner)

    return router, SimpleNamespace(
        health=health,
        skills=skills,
        create_session=create_session,
        get_session=get_session,
        act=act,
        traces=traces,
        replay=replay,
        progress=progress,
        report=report,
        original_image=original_image,
        correct=correct,
        correct_open=correct_open,
        open_image=open_image,
        publish_assignment=publish_assignment,
        teaching_dashboard=teaching_dashboard,
        teaching_memory=teaching_memory,
        confirm_review=confirm_review,
        publish_path=publish_path,
        next_learning=next_learning,
        assign_followups=assign_followups,
        learning_followups=learning_followups,
        revise_followup=revise_followup,
        save_pack=save_pack,
        generate_pack=generate_pack,
        list_packs=list_packs,
        get_pack=get_pack,
        update_pack=update_pack,
        publish_pack=publish_pack,
        learning_inbox=learning_inbox,
    )
