"""HTTP request contracts and routes.

Every ``/api`` route takes a :class:`~harness.interfaces.access.Guard` and calls one
of its checks first; see ``access.py`` for the two modes (local demo / classroom).
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from harness.core.pedagogy import PROFILES
from harness.errors import Conflict
from harness.grading.criteria import bind_algebra
from harness.interfaces.access import Guard, classroom_mode, current_account
from harness.legacy.course import SKILLS
from harness.llm.model import ModelUnavailable
from harness.resources.materials import MAX_BYTES, read_material
from harness.teaching.planning import Curriculum, FollowupBatch
from harness.teaching.preparation import Distribution, GeneratePack, Pack
from harness.teaching.teacher import Assignment

CRITERION_KINDS = {
    'model': '开放评分（模型）',
    'algebra': '代数等价',
    'numeric': '数值',
    'unit': '指定单位',
    'explanation': '包含解释',
    'factorization': '因式分解形式',
    'source_quote': '引用材料原文',
    'word_count': '字数范围',
}


class NewSession(BaseModel):
    learner: str = Field(min_length=1, max_length=80)
    skill: str | None = None
    task: str = Field(default='', max_length=1600)
    material: str = Field(default='', max_length=100000)
    mode: str = 'problem'
    image_url: str = Field(default='', max_length=12_000_000)
    request_id: str | None = None
    assignment_id: str | None = None
    followup_id: str | None = None


class Operation(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    version: int | None = None
    text: str = Field(default='', max_length=3000)
    image_url: str = Field(default='', max_length=12_000_000)


class Correction(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    status: str
    reason: str = Field(min_length=1, max_length=1000)


class FollowupRequest(FollowupBatch):
    request_id: str = Field(min_length=1, max_length=100)


class FollowupRevision(BaseModel):
    action: Literal['postpone', 'cancel']
    due_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=400)
    request_id: str = Field(min_length=1, max_length=100)


class ReviewConfirmation(BaseModel):
    reason: str = Field(min_length=1, max_length=400)
    request_id: str = Field(min_length=1, max_length=100)


class PackSave(Pack):
    request_id: str = Field(min_length=1, max_length=100)
    version: int | None = Field(default=None, ge=1)


class PackGenerate(GeneratePack):
    request_id: str = Field(min_length=1, max_length=100)


class PackPublish(Distribution):
    request_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)


class AlgebraPreview(BaseModel):
    question: str = Field(min_length=1, max_length=900)
    initial: str = Field(default='', max_length=160)
    kind: Literal['none', 'expression', 'equation'] = 'none'


OPERATIONS = ('attempt', 'hint', 'image', 'confirm', 'reply', 'explain', 'check', 'resume_practice', 'confirm_task')
BODYLESS_OPERATIONS = ('hint', 'explain', 'check', 'resume_practice')


def call(fn, *args, **kwargs):
    """Map domain errors to HTTP status codes."""
    try:
        return fn(*args, **kwargs)
    except ModelUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except Conflict as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def create_router(get_engine):
    """Resolve the gateway per call so each app can own an isolated runtime."""
    router = APIRouter()

    def guard(request: Request) -> Guard:
        return Guard(current_account(request), get_engine())

    Allowed = Depends(guard)

    @router.get('/health')
    def health():
        if classroom_mode():
            return {'ready': True, 'mode': 'classroom-api'}
        return {'ready': True, 'mode': 'local-demo', 'model_base_url': get_engine().model.base_url}

    # ---- identity and reference data -------------------------------------------------

    @router.get('/api/me')
    def me(g: Guard = Allowed):
        g.any()
        if not g.account:
            return {'mode': 'local-demo'}
        a = g.account
        return {'mode': 'classroom', 'role': a.role, 'class_id': a.class_id, 'learner': a.learner}

    @router.get('/api/meta')
    def meta(g: Guard = Allowed):
        g.any()
        return {
            'task_types': {k: v['label'] for k, v in PROFILES.items()},
            'criterion_kinds': CRITERION_KINDS,
            'policy': get_engine().open.policy,
            'model': os.getenv('OMNIEDU_MODEL', 'omniedu-9b'),
            'knowledge_available': get_engine().open.knowledge.available(),
            'cross_review': get_engine().open.reviewer is not None,
        }

    @router.post('/api/materials/extract')
    async def extract_material(file: UploadFile = File(...), g: Guard = Allowed):  # noqa: B008
        g.any()
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in ('.txt', '.md', '.pdf'):
            raise HTTPException(400, '材料须为 .txt、.md 或文字版 .pdf')
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(400, '材料文件需小于20MB')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ('material' + suffix)
            path.write_bytes(data)
            return {'text': call(read_material, path), 'filename': file.filename}

    # ---- example courses (legacy; local demo only) -----------------------------------

    @router.get('/api/skills')
    def skills(g: Guard = Allowed):
        g.local_only()
        return list(SKILLS.values())

    @router.get('/api/progress/{learner}')
    def progress(learner: str, g: Guard = Allowed):
        g.local_only()
        return call(get_engine().progress, learner)

    @router.get('/api/teacher/report')
    def report(learner: str | None = None, g: Guard = Allowed):
        g.local_only()
        return call(get_engine().report, learner)

    @router.get('/api/teacher/images/{event_id}')
    def original_image(event_id: int, g: Guard = Allowed):
        g.local_only()
        with get_engine().connect() as c:
            row = c.execute("SELECT payload FROM events WHERE id=? AND type='image'", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return json.loads(row['payload'])

    @router.post('/api/teacher/attempts/{event_id}/corrections')
    def correct(event_id: int, req: Correction, g: Guard = Allowed):
        g.local_only()
        return call(get_engine().correct, event_id, req.status, req.reason, req.request_id)

    # ---- learner sessions ------------------------------------------------------------

    @router.post('/api/sessions')
    def create_session(req: NewSession, g: Guard = Allowed):
        g.new_session(req.learner, req.assignment_id, req.followup_id, req.skill)
        if req.skill:
            if req.task or req.material or req.image_url:
                raise HTTPException(400, '示例课程与自定义任务请分开创建')
            return call(get_engine().create, req.learner, req.skill)
        return call(
            get_engine().create_task,
            learner=req.learner,
            request=req.task,
            material=req.material,
            mode=req.mode,
            image_url=req.image_url,
            request_id=req.request_id,
            assignment_id=req.assignment_id,
            followup_id=req.followup_id,
        )

    @router.get('/api/sessions/{sid}')
    def get_session(sid: str, g: Guard = Allowed):
        g.session(sid)
        return call(get_engine().session, sid)

    @router.post('/api/sessions/{sid}/{action}')
    def act(sid: str, action: str, req: Operation, g: Guard = Allowed):
        g.session(sid)
        if action not in OPERATIONS:
            raise HTTPException(404)
        if action == 'image':
            body = {'image_url': req.image_url}
        elif action in BODYLESS_OPERATIONS:
            body = {}
        else:
            body = {'text': req.text}
        return call(get_engine().operation, sid, req.request_id, action, body, req.version)

    @router.get('/api/learning/inbox')
    def learning_inbox(class_id: str, learner: str, g: Guard = Allowed):
        g.member(class_id, learner)
        return call(get_engine().open.inbox, class_id, learner)

    @router.get('/api/learning/followups')
    def learning_followups(class_id: str, learner: str, g: Guard = Allowed):
        g.member(class_id, learner)
        return call(get_engine().open.followups, class_id, learner)

    @router.get('/api/learning/next')
    def next_learning(path_id: str, learner: str, g: Guard = Allowed):
        g.path(path_id, learner)
        return call(get_engine().open.next_step, path_id, learner)

    # ---- teacher: sessions, evidence and review --------------------------------------

    @router.get('/api/teacher/sessions/{sid}/trace')
    def traces(sid: str, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        return call(get_engine().traces, sid)

    @router.get('/api/teacher/sessions/{sid}/replay')
    def replay(sid: str, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        return call(get_engine().replay, sid)

    @router.get('/api/teacher/tasks/{sid}/attempts/{event_id}')
    def review_detail(sid: str, event_id: int, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        return call(get_engine().open.review_detail, sid, event_id)

    @router.post('/api/teacher/tasks/{sid}/attempts/{event_id}/corrections')
    def correct_open(sid: str, event_id: int, req: Correction, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        return call(get_engine().open.correct, sid, event_id, req.status, req.reason, req.request_id)

    @router.post('/api/teacher/tasks/{sid}/attempts/{event_id}/reviews')
    def confirm_review(sid: str, event_id: int, req: ReviewConfirmation, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        return call(get_engine().open.confirm_review, sid, event_id, req.reason, req.request_id)

    @router.get('/api/teacher/tasks/{sid}/images/{event_id}')
    def open_image(sid: str, event_id: int, g: Guard = Allowed):
        g.session(sid, teacher_only=True)
        payload = get_engine().open.image_event(sid, event_id)
        if not payload:
            raise HTTPException(404)
        return payload

    @router.get('/api/teacher/dashboard')
    def teaching_dashboard(class_id: str | None = None, g: Guard = Allowed):
        g.teacher(class_id)
        return call(get_engine().open.dashboard, class_id)

    @router.get('/api/teacher/memory/{learner}')
    def teaching_memory(learner: str, class_id: str | None = None, g: Guard = Allowed):
        g.teacher(class_id)
        return call(get_engine().open.learning_memory, learner, class_id)

    # ---- teacher: authoring ----------------------------------------------------------

    @router.post('/api/teacher/assignments')
    def publish_assignment(req: Assignment, g: Guard = Allowed):
        g.teacher(req.class_id)
        return call(get_engine().open.publish_assignment, req.model_dump())

    @router.get('/api/teacher/classes')
    def list_classes(g: Guard = Allowed):
        g.any_teacher()
        if g.account:
            return [g.account.class_id]
        return get_engine().open.classes()

    @router.get('/api/teacher/assignments')
    def list_assignments(class_id: str, g: Guard = Allowed):
        g.teacher(class_id)
        return call(get_engine().open.assignments, class_id)

    @router.post('/api/teacher/algebra/preview')
    def preview_algebra(req: AlgebraPreview, g: Guard = Allowed):
        g.any_teacher()
        return call(bind_algebra, req.question, req.initial, req.kind)

    @router.get('/api/teacher/knowledge/suggest')
    def suggest_knowledge(question: str, subject: str = '', stage: str = '', g: Guard = Allowed):
        g.any_teacher()
        return call(get_engine().open.suggest_knowledge, question, subject.strip(), stage or None)

    @router.get('/api/teacher/knowledge/nodes/{node_id}')
    def knowledge_node(node_id: str, g: Guard = Allowed):
        g.any_teacher()
        node = get_engine().open.knowledge_node(node_id)
        if not node:
            raise HTTPException(404, '知识点库中没有这个编号')
        return node

    @router.post('/api/teacher/paths')
    def publish_path(req: Curriculum, g: Guard = Allowed):
        g.teacher(req.class_id)
        return call(get_engine().open.publish_path, req.model_dump())

    @router.post('/api/teacher/followups')
    def assign_followups(req: FollowupRequest, g: Guard = Allowed):
        g.teacher(req.class_id)
        return call(get_engine().open.assign_followups, req.model_dump(exclude={'request_id'}), req.request_id)

    @router.patch('/api/teacher/followups/{key}')
    def revise_followup(key: str, req: FollowupRevision, g: Guard = Allowed):
        g.followup(key)
        return call(get_engine().open.revise_followup, key, req.action, req.due_at, req.reason, req.request_id)

    @router.post('/api/teacher/packs')
    def save_pack(req: PackSave, g: Guard = Allowed):
        g.teacher(req.class_id)
        return call(get_engine().open.save_pack, req.model_dump(exclude={'request_id', 'version'}), req.request_id)

    @router.post('/api/teacher/packs/generate')
    def generate_pack(req: PackGenerate, g: Guard = Allowed):
        g.teacher(req.class_id)
        return call(get_engine().open.generate_pack, req.model_dump(exclude={'request_id'}), req.request_id)

    @router.get('/api/teacher/packs')
    def list_packs(class_id: str, g: Guard = Allowed):
        g.teacher(class_id)
        return call(get_engine().open.packs, class_id)

    @router.get('/api/teacher/packs/{pack_id}')
    def get_pack(pack_id: str, g: Guard = Allowed):
        g.pack(pack_id)
        return call(get_engine().open.load_pack, pack_id)

    @router.put('/api/teacher/packs/{pack_id}')
    def update_pack(pack_id: str, req: PackSave, g: Guard = Allowed):
        g.pack(pack_id)
        g.teacher(req.class_id)
        return call(
            get_engine().open.save_pack,
            req.model_dump(exclude={'request_id', 'version'}),
            req.request_id,
            pack_id,
            req.version,
        )

    @router.post('/api/teacher/packs/{pack_id}/publish')
    def publish_pack(pack_id: str, req: PackPublish, g: Guard = Allowed):
        g.pack(pack_id)
        return call(
            get_engine().open.publish_pack,
            pack_id,
            req.version,
            req.model_dump(exclude={'request_id', 'version'}),
            req.request_id,
        )

    return router
