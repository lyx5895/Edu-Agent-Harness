"""HTTP API and static teacher workspace for edu_core.

Local single-user tool for now: no accounts (bind to 127.0.0.1). Model calls are synchronous; FastAPI
runs these handlers in its thread pool.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from edu_core.config import load_settings
from edu_core.context import ContextViolation
from edu_core.llm import Gateway, ModelUnavailable
from edu_core.storage import Store
from edu_core.subjects import QUESTION_TYPES, SUBJECTS, error_tags
from edu_core.workflow import Conflict, Workspace

STATIC = Path(__file__).parent / 'static'
log = logging.getLogger('edu_app')


class NewProject(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    subject: str
    grade: str = ''
    notes: str = Field(default='', max_length=2000)
    knowledge_module: str = Field(default='', max_length=200)
    lesson: str = Field(default='', max_length=100)
    learner_profile: str = Field(default='', max_length=500)


class NextLesson(BaseModel):
    knowledge_module: str | None = Field(default=None, max_length=200)
    lesson: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    subject: str | None = None
    grade: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    knowledge_module: str | None = Field(default=None, max_length=200)
    lesson: str | None = Field(default=None, max_length=100)
    learner_profile: str | None = Field(default=None, max_length=500)
    lesson_scope: str | None = Field(default=None, max_length=200)
    curriculum_ids: list[str] | None = Field(default=None, max_length=30)
    curriculum_off: bool | None = None


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    filename: str = '粘贴文本'


class ContentBody(BaseModel):
    content: dict[str, Any]


class ConfirmBody(BaseModel):
    change_kind: str = 'substantive'


class GenerateBody(BaseModel):
    types: list[str] | None = None
    count: int = Field(default=2, ge=1, le=5)
    difficulty: str = '中等'
    notes: str = Field(default='', max_length=1000)
    ab: bool | None = None
    purpose: str = Field(default='课堂练习', max_length=40)
    objective_ids: list[str] | None = None


class ManualQuestion(BaseModel):
    question: dict[str, Any]
    rubric: dict[str, Any]
    round: int = 1


class RegenerateBody(BaseModel):
    instruction: str = Field(default='', max_length=300)
    difficulty: Literal['基础', '中等', '较难'] | None = None


class LearnersBody(BaseModel):
    aliases: list[str] = Field(max_length=500)


class SubmissionsBody(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)
    round: int = 1
    add_missing: bool = False
    assignment_id: str | None = None


class PhotoAnswerIn(BaseModel):
    question_no: int
    text: str = Field(default='', max_length=4000)


class PhotoAnswersBody(BaseModel):
    learner: str = Field(min_length=1, max_length=40)
    round: int = 1
    answers: list[PhotoAnswerIn] = Field(min_length=1, max_length=200)
    photos: list[str] = Field(default_factory=list, max_length=8)
    add_missing: bool = False
    generation_id: str | None = Field(default=None, max_length=64)
    assignment_id: str | None = None


class RoundBody(BaseModel):
    round: int = 1
    regrade: bool = False
    assignment_id: str | None = None
    partial: bool = False


class OverrideBody(BaseModel):
    points: list[dict[str, Any]]
    note: str = Field(default='', max_length=1000)


class ConfirmGradings(BaseModel):
    round: int = 1
    ids: list[str] | None = None
    assignment_id: str | None = None


class PracticeBody(BaseModel):
    pattern_ids: list[str] | None = None
    count: int = Field(default=1, ge=1, le=3)
    round: int | None = None
    assignment_id: str | None = None


class ReviewBody(BaseModel):
    round: int | None = None
    pattern_ids: list[str] | None = None
    assignment_id: str | None = None


class CompareBody(BaseModel):
    round_a: int = 1
    round_b: int = 2
    assignment_id: str | None = None


class ResolveBody(BaseModel):
    resolution: str


class NewCourse(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=20)
    grade: str = Field(default='', max_length=40)
    term: str = Field(default='', max_length=40)
    textbook: str = Field(default='', max_length=200)
    notes: str = Field(default='', max_length=2000)


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    grade: str | None = Field(default=None, max_length=40)
    term: str | None = Field(default=None, max_length=40)
    textbook: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    archived: bool | None = None


class NewClass(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    aliases: list[str] = Field(default_factory=list, max_length=500)
    notes: str = Field(default='', max_length=500)


class LinkClass(BaseModel):
    class_id: str


class NewTask(BaseModel):
    kind: Literal['prep', 'practice', 'grading', 'explain']
    title: str = Field(default='', max_length=120)
    offering_id: str = ''
    request: str = Field(default='', max_length=2000)
    knowledge_module: str = Field(default='', max_length=200)
    lesson: str = Field(default='', max_length=100)
    lesson_scope: str = Field(default='', max_length=200)
    unit_id: str | None = None


class TaskUpdate(BaseModel):
    status: Literal['open', 'done', 'archived'] | None = None
    title: str | None = Field(default=None, max_length=120)


class SessionUpdate(BaseModel):
    status: Literal['planned', 'taught', 'cancelled'] | None = None
    planned_date: str | None = Field(default=None, max_length=10)
    title: str | None = Field(default=None, max_length=120)


class OfferingBody(BaseModel):
    offering_id: str


class AssignBody(BaseModel):
    offering_id: str
    round: int = 1
    title: str = Field(default='', max_length=120)
    parent_id: str = ''


class CorrectRubric(BaseModel):
    question_lineage: str
    content: dict[str, Any]
    note: str = Field(default='', max_length=500)


class RepinBody(BaseModel):
    question_lineage: str


class UnitBody(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class ProjectUnit(BaseModel):
    unit_id: str = ''


class ExplainBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    ask: str = Field(default='', max_length=500)
    student_answer: str = Field(default='', max_length=4000)


class DeriveBody(BaseModel):
    offering_id: str
    title: str = Field(default='', max_length=120)


class StatusPrefs(BaseModel):
    exclude: list[str] | None = Field(default=None, max_length=100)
    exclude_summary: bool | None = None


class RelinkBody(BaseModel):
    artifact_id: str
    plan_objective_id: str
    objective_id: str = ''


class UnitReviewBody(BaseModel):
    offering_id: str
    assignment_ids: list[str] = Field(min_length=1, max_length=20)
    pattern_ids: list[str] | None = None
    title: str = Field(default='', max_length=120)


class ReviseBody(BaseModel):
    target: str = Field(min_length=1, max_length=40)
    instruction: str = Field(min_length=1, max_length=300)


class MaterialPrefs(BaseModel):
    exclude: list[str] | None = Field(default=None, max_length=100)
    focus: dict[str, str] | None = None


class CoursewareBody(BaseModel):
    package_id: str
    kind: Literal['handout', 'slides', 'video', 'other']
    files: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    external_run_id: str = Field(default='', max_length=120)
    note: str = Field(default='', max_length=500)


class ChoiceBody2(BaseModel):
    group: str
    verdict: str
    reason: str = Field(default='', max_length=500)
    work_choice: Literal['A', 'B'] | None = None
    preference: Literal['better', 'work_only', 'none'] = 'none'
    reason_tags: list[str] = Field(default_factory=list, max_length=6)


class MergeBody(BaseModel):
    question: dict[str, Any]
    rubric: dict[str, Any]
    sources: list[str] = Field(min_length=2, max_length=10)


class MapBody(BaseModel):
    project_ids: list[str] = Field(min_length=1, max_length=200)
    course_id: str | None = None
    course: dict[str, str] | None = None
    class_id: str | None = None
    class_name: str | None = Field(default=None, max_length=60)


def default_workspace() -> Workspace:
    settings = load_settings()
    llm = None
    if settings.api_key:
        llm = Gateway(
            settings.base_url,
            settings.api_key,
            settings.models,
            timeout=settings.timeout,
            max_tokens=settings.max_tokens,
        )
    else:
        log.warning('EDU_LLM_API_KEY is not set: model features are disabled')
    return Workspace(Store(settings.db), llm, settings)


def read_upload(path: Path, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in ('.txt', '.md', '.csv'):
        return path.read_text(encoding='utf-8')
    if suffix == '.pdf':
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            text = '\n\n'.join((p.extract_text() or '') for p in PdfReader(path).pages)
        except PdfReadError as e:
            raise ValueError(f'无法读取 PDF：{e}') from None
        if not text.strip():
            raise ValueError('PDF 中没有可提取的文字（可能是扫描件）')
        return text
    raise ValueError('只支持 .txt、.md、.csv 和文字版 PDF')


def is_local_client(host: str | None) -> bool:
    """Loopback peers only. 'testclient' is Starlette's in-process TestClient, never a network peer."""
    if host == 'testclient':
        return True
    try:
        return ipaddress.ip_address(host or '').is_loopback
    except ValueError:
        return False


def create_app(workspace: Workspace | None = None) -> FastAPI:
    app = FastAPI(title='Edu-Agent 教师端', version='0.11.0-dev')
    state = {'ws': workspace}

    def ws() -> Workspace:
        if state['ws'] is None:
            state['ws'] = default_workspace()
        return state['ws']

    def once(request: Request, scope: str, fn):
        """Idempotency-Key header (design doc 8): a retried request returns the first result instead of redoing it."""
        return ws().once(request.headers.get('Idempotency-Key', '').strip()[:120], scope, fn)

    @app.exception_handler(KeyError)
    async def not_found(_: Request, e: KeyError):
        return JSONResponse({'detail': str(e).strip("'")}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_: Request, e: Conflict):
        return JSONResponse({'detail': str(e)}, status_code=409)

    @app.exception_handler(ContextViolation)
    async def context(_: Request, e: ContextViolation):
        return JSONResponse({'detail': str(e)}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad(_: Request, e: ValueError):
        return JSONResponse({'detail': str(e)}, status_code=400)

    @app.exception_handler(ModelUnavailable)
    async def unavailable(_: Request, e: ModelUnavailable):
        return JSONResponse({'detail': str(e)}, status_code=503)

    @app.get('/api/meta')
    def meta():
        w = ws()
        return {
            'subjects': [
                {'id': p['id'], 'name': p['name'], 'types': p['types'], 'tags': error_tags(p['id'])}
                for p in SUBJECTS.values()
            ],
            'question_types': QUESTION_TYPES,
            'common_tags': error_tags('通用'),
            'models': w.settings.models,
            'model_ready': w.llm is not None,
        }

    @app.get('/api/projects')
    def projects():
        return ws().projects()

    @app.post('/api/projects')
    def create_project(body: NewProject):
        return ws().create_project(
            body.title,
            body.subject,
            body.grade,
            body.notes,
            knowledge_module=body.knowledge_module,
            lesson=body.lesson,
            learner_profile=body.learner_profile,
        )

    @app.get('/api/projects/{pid}')
    def overview(pid: str, assignment_id: str | None = None):
        return ws().overview(pid, assignment_id or None)

    @app.patch('/api/projects/{pid}')
    def update_project(pid: str, body: ProjectUpdate):
        return ws().update_project(pid, **body.model_dump())

    @app.get('/api/projects/{pid}/curriculum')
    def curriculum(pid: str, q: str | None = None):
        return ws().curriculum(pid, q)

    @app.post('/api/projects/{pid}/next-lesson')
    def next_lesson(pid: str, body: NextLesson):
        return ws().next_lesson(pid, **body.model_dump())

    @app.get('/api/projects/{pid}/materials')
    def materials(pid: str):
        return ws().materials(pid)

    @app.post('/api/projects/{pid}/materials')
    def add_material(pid: str, body: TextBody):
        return ws().add_material(pid, body.text, body.filename)

    @app.post('/api/projects/{pid}/materials/upload')
    async def upload_material(pid: str, file: UploadFile = File(...)):  # noqa: B008
        data = await file.read()
        if len(data) > 20 * 1024**2:
            raise ValueError('文件需小于 20MB')
        with tempfile.NamedTemporaryFile(suffix=Path(file.filename or 'x.txt').suffix) as tmp:
            tmp.write(data)
            tmp.flush()
            text = read_upload(Path(tmp.name), file.filename or 'x.txt')
        return ws().add_material(pid, text, file.filename or '上传文件', kind='file')

    @app.get('/api/projects/{pid}/artifacts')
    def artifacts(pid: str, type: str, round: int | None = None):
        from edu_core import artifacts as A

        with ws().store.read() as c:
            return A.current(c, pid, type, round_=round)

    @app.get('/api/artifacts/{aid}')
    def artifact(aid: str):
        return ws().artifact(aid)

    @app.put('/api/artifacts/{aid}')
    def edit(aid: str, body: ContentBody):
        return ws().edit(aid, body.content)

    @app.post('/api/artifacts/{aid}/confirm')
    def confirm(aid: str, body: ConfirmBody):
        return ws().confirm(aid, body.change_kind)

    @app.post('/api/projects/{pid}/questions/generate')
    def generate(pid: str, body: GenerateBody, request: Request):
        return once(
            request,
            f'generate:{pid}',
            lambda: ws().generate_questions(
                pid,
                types=body.types,
                count=body.count,
                difficulty=body.difficulty,
                objective_ids=body.objective_ids,
                notes=body.notes,
                ab=body.ab,
                purpose=body.purpose,
            ),
        )

    @app.post('/api/projects/{pid}/questions')
    def manual_question(pid: str, body: ManualQuestion):
        return ws().create_question(pid, body.question, body.rubric, round_=body.round)

    @app.get('/api/projects/{pid}/questions')
    def questions(pid: str, round: int | None = None):
        return ws().questions(pid, round)

    @app.get('/api/projects/{pid}/coverage')
    def coverage(pid: str, round: int = 1):
        return ws().coverage(pid, round)

    @app.post('/api/questions/{qid}/adopt-solver')
    def adopt_solver(qid: str):
        return ws().adopt_solver_answer(qid)

    @app.post('/api/questions/{qid}/regenerate')
    def regenerate(qid: str, body: RegenerateBody, request: Request):
        return once(
            request, f'regenerate:{qid}', lambda: ws().regenerate_question(qid, body.instruction, body.difficulty)
        )

    @app.post('/api/projects/{pid}/choices')
    def choose(pid: str, body: ChoiceBody2):
        return ws().choose(
            pid,
            body.group,
            body.verdict,
            body.reason,
            work_choice=body.work_choice,
            preference=body.preference,
            reason_tags=body.reason_tags,
        )

    @app.post('/api/choices/{choice_id}/revoke')
    def revoke_choice(choice_id: str):
        return ws().revoke_choice(choice_id)

    @app.post('/api/projects/{pid}/choices/{group}/merge')
    def merge_candidates(pid: str, group: str, body: MergeBody):
        return ws().merge_candidates(pid, group, body.question, body.rubric, body.sources)

    @app.post('/api/projects/{pid}/import-paper')
    def import_paper(pid: str, body: TextBody, request: Request):
        return once(request, f'import-paper:{pid}', lambda: ws().import_paper(pid, body.text, body.filename))

    @app.get('/api/projects/{pid}/learners')
    def learners(pid: str):
        return ws().learners(pid)

    @app.post('/api/projects/{pid}/learners')
    def add_learners(pid: str, body: LearnersBody):
        return ws().add_learners(pid, body.aliases)

    @app.post('/api/projects/{pid}/submissions')
    def import_submissions(pid: str, body: SubmissionsBody, request: Request):
        return once(
            request,
            f'submissions:{pid}',
            lambda: ws().import_submissions(
                pid, body.csv, body.round, body.add_missing, assignment_id=body.assignment_id
            ),
        )

    @app.get('/api/projects/{pid}/submissions')
    def submissions(pid: str, round: int = 1, assignment_id: str | None = None):
        return ws().submissions(pid, round, assignment_id=assignment_id or None)

    @app.post('/api/projects/{pid}/photo-answers/transcribe')
    async def transcribe_photos(
        pid: str,
        learner: str = Form(...),
        round: int = Form(1),
        assignment_id: str = Form(''),
        files: list[UploadFile] = File(...),  # noqa: B008
    ):
        photos = [await f.read() for f in files]
        # The model call takes seconds: run it off the event loop, like the sync routes.
        return await run_in_threadpool(ws().transcribe_photos, pid, learner, photos, round, assignment_id or None)

    @app.post('/api/projects/{pid}/photo-answers')
    def import_photo_answers(pid: str, body: PhotoAnswersBody, request: Request):
        return once(
            request,
            f'photo-answers:{pid}',
            lambda: ws().import_photo_answers(
                pid,
                body.learner,
                [a.model_dump() for a in body.answers],
                photos=body.photos,
                round_=body.round,
                add_missing=body.add_missing,
                generation_id=body.generation_id,
                assignment_id=body.assignment_id,
            ),
        )

    @app.get('/api/photos/{name}')
    def photo(name: str):
        return FileResponse(
            ws().photo_path(name), media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=86400'}
        )

    @app.post('/api/projects/{pid}/grade')
    def grade(pid: str, body: RoundBody, request: Request):
        return once(
            request,
            f'grade:{pid}',
            lambda: ws().grade_round(pid, body.round, body.regrade, assignment_id=body.assignment_id),
        )

    @app.get('/api/projects/{pid}/gradings')
    def gradings(pid: str, round: int = 1, assignment_id: str | None = None):
        return ws().gradings(pid, round, assignment_id=assignment_id or None)

    @app.get('/api/projects/{pid}/review-queue')
    def queue(pid: str, round: int = 1, assignment_id: str | None = None):
        return ws().review_queue(pid, round, assignment_id=assignment_id or None)

    @app.post('/api/gradings/{gid}/override')
    def override(gid: str, body: OverrideBody):
        return ws().override(gid, body.points, body.note)

    @app.post('/api/projects/{pid}/gradings/confirm')
    def confirm_gradings(pid: str, body: ConfirmGradings):
        return ws().confirm_gradings(pid, body.round, body.ids, assignment_id=body.assignment_id)

    @app.post('/api/projects/{pid}/analyze')
    def analyze(pid: str, body: RoundBody, request: Request):
        return once(
            request,
            f'analyze:{pid}',
            lambda: ws().analyze(pid, body.round, assignment_id=body.assignment_id, partial=body.partial),
        )

    @app.get('/api/projects/{pid}/analyses')
    def analyses(pid: str, assignment_id: str | None = None):
        return ws().analyses(pid, assignment_id or None)

    @app.get('/api/projects/{pid}/patterns')
    def patterns(pid: str, assignment_id: str | None = None):
        return ws().patterns(pid, assignment_id or None)

    @app.post('/api/projects/{pid}/practice')
    def practice(pid: str, body: PracticeBody, request: Request):
        return once(
            request,
            f'practice:{pid}',
            lambda: ws().practice(
                pid, body.pattern_ids, body.count, round_=body.round, assignment_id=body.assignment_id
            ),
        )

    @app.post('/api/projects/{pid}/lesson-plan/draft')
    def draft_lesson_plan(pid: str, request: Request):
        return once(request, f'lesson-plan:{pid}', lambda: ws().draft_lesson_plan(pid))

    @app.get('/api/projects/{pid}/lesson-plans')
    def lesson_plans(pid: str):
        return ws().lesson_plans(pid)

    @app.post('/api/projects/{pid}/review/draft')
    def draft_review(pid: str, body: ReviewBody, request: Request):
        return once(
            request,
            f'review:{pid}',
            lambda: ws().draft_review(pid, body.round, body.pattern_ids, assignment_id=body.assignment_id),
        )

    @app.get('/api/projects/{pid}/review-plans')
    def review_plans(pid: str, round: int | None = None, assignment_id: str | None = None):
        return ws().review_plans(pid, round, assignment_id=assignment_id or None)

    @app.post('/api/projects/{pid}/compare')
    def compare(pid: str, body: CompareBody, request: Request):
        return once(
            request,
            f'compare:{pid}',
            lambda: ws().compare(pid, body.round_a, body.round_b, assignment_id=body.assignment_id),
        )

    @app.get('/api/projects/{pid}/comparisons')
    def comparisons(pid: str, assignment_id: str | None = None):
        from edu_core import artifacts as A

        with ws().store.read() as c:
            return A.current(c, pid, 'comparison', assignment=assignment_id or '')

    # ----- course workspace (COURSE_WORKSPACE_DESIGN.md) -----

    @app.get('/api/home')
    def home(course_id: str | None = None):
        return ws().home(course_id)

    @app.get('/api/courses')
    def courses(include_archived: bool = False):
        return ws().courses(include_archived)

    @app.post('/api/courses')
    def create_course(body: NewCourse):
        return ws().create_course(**body.model_dump())

    @app.get('/api/courses/{cid}')
    def course(cid: str):
        return ws().course(cid)

    @app.patch('/api/courses/{cid}')
    def update_course(cid: str, body: CourseUpdate):
        return ws().update_course(cid, **body.model_dump())

    @app.post('/api/courses/{cid}/materials')
    def add_course_material(cid: str, body: TextBody):
        return ws().add_material(None, body.text, body.filename, course_id=cid)

    @app.post('/api/courses/{cid}/classes')
    def link_class(cid: str, body: LinkClass):
        return ws().link_class(cid, body.class_id)

    @app.post('/api/courses/{cid}/tasks')
    def start_task(cid: str, body: NewTask, request: Request):
        return once(request, f'task:{cid}', lambda: ws().start_task(cid, **body.model_dump()))

    @app.get('/api/tasks')
    def tasks(course_id: str | None = None, status: str | None = 'open'):
        return ws().tasks(course_id, status or None)

    @app.patch('/api/tasks/{tid}')
    def update_task(tid: str, body: TaskUpdate):
        return ws().update_task(tid, **body.model_dump())

    @app.get('/api/classes')
    def classes():
        return ws().classes()

    @app.post('/api/classes')
    def create_class(body: NewClass):
        return ws().create_class(body.name, body.aliases, body.notes)

    @app.get('/api/classes/{class_id}')
    def class_detail(class_id: str):
        return ws().class_detail(class_id)

    @app.post('/api/classes/{class_id}/members')
    def add_members(class_id: str, body: LearnersBody):
        return ws().add_members(class_id, body.aliases)

    @app.delete('/api/classes/{class_id}/members/{member_id}')
    def remove_member(class_id: str, member_id: str):
        return ws().remove_member(class_id, member_id)

    @app.get('/api/offerings/{oid}/sessions')
    def sessions(oid: str):
        return ws().sessions(oid)

    @app.get('/api/offerings/{oid}/next-lesson')
    def suggest_next_lesson(oid: str):
        return ws().suggest_next_lesson(oid)

    @app.get('/api/offerings/{oid}/status')
    def class_status(oid: str):
        return ws().class_status(oid)

    @app.get('/api/offerings/{oid}/assignments')
    def offering_assignments(oid: str):
        return ws().assignments(offering_id=oid)

    @app.patch('/api/sessions/{sid}')
    def update_session(sid: str, body: SessionUpdate):
        return ws().update_session(sid, **body.model_dump())

    @app.post('/api/projects/{pid}/use-plan')
    def use_plan(pid: str, body: OfferingBody):
        return ws().use_plan_for_class(pid, body.offering_id)

    @app.get('/api/projects/{pid}/assignments')
    def project_assignments(pid: str):
        return ws().assignments(pid)

    @app.post('/api/projects/{pid}/assignments')
    def assign(pid: str, body: AssignBody, request: Request):
        return once(
            request,
            f'assign:{pid}',
            lambda: ws().assign(pid, body.offering_id, body.round, body.title, body.parent_id),
        )

    @app.get('/api/assignments/{aid}')
    def assignment(aid: str):
        return ws().assignment(aid)

    @app.get('/api/assignments/{aid}/correction')
    def correction_impact(aid: str, question_lineage: str):
        return ws().correction_impact(aid, question_lineage)

    @app.post('/api/assignments/{aid}/correct-rubric')
    def correct_rubric(aid: str, body: CorrectRubric, request: Request):
        return once(
            request,
            f'correct:{aid}',
            lambda: ws().correct_rubric(aid, body.question_lineage, body.content, body.note),
        )

    @app.post('/api/assignments/{aid}/repin')
    def repin(aid: str, body: RepinBody):
        return ws().repin(aid, body.question_lineage)

    @app.get('/api/courses/{cid}/units')
    def units(cid: str):
        return ws().units(cid)

    @app.post('/api/courses/{cid}/units')
    def create_unit(cid: str, body: UnitBody):
        return ws().create_unit(cid, body.title)

    @app.get('/api/units/{unit_id}')
    def unit_view(unit_id: str):
        return ws().unit_view(unit_id)

    @app.patch('/api/units/{unit_id}')
    def rename_unit(unit_id: str, body: UnitBody):
        return ws().rename_unit(unit_id, body.title)

    @app.put('/api/projects/{pid}/unit')
    def set_project_unit(pid: str, body: ProjectUnit):
        return ws().set_project_unit(pid, body.unit_id)

    @app.post('/api/projects/{pid}/explain')
    def explain(pid: str, body: ExplainBody, request: Request):
        return once(request, f'explain:{pid}', lambda: ws().explain(pid, body.question, body.ask, body.student_answer))

    @app.get('/api/projects/{pid}/explanations')
    def explanations(pid: str):
        return ws().explanations(pid)

    @app.post('/api/projects/{pid}/derive')
    def derive(pid: str, body: DeriveBody, request: Request):
        return once(request, f'derive:{pid}', lambda: ws().derive_for_class(pid, body.offering_id, body.title))

    @app.get('/api/projects/{pid}/class-status')
    def task_class_status(pid: str):
        return ws().task_class_status(pid)

    @app.patch('/api/projects/{pid}/class-status')
    def set_status_prefs(pid: str, body: StatusPrefs):
        return ws().set_status_prefs(pid, body.exclude, body.exclude_summary)

    # ----- course objectives, unit review -----

    @app.get('/api/courses/{cid}/objectives')
    def course_objectives(cid: str, unit_id: str | None = None):
        return ws().course_objectives(cid, unit_id)

    @app.get('/api/objectives/{objective_id}')
    def objective_view(objective_id: str):
        return ws().objective_view(objective_id)

    @app.post('/api/objectives/{objective_id}/reviewed')
    def objective_reviewed(objective_id: str):
        return ws().mark_objective_reviewed(objective_id)

    @app.post('/api/objective-links')
    def relink_objective(body: RelinkBody):
        return ws().relink_objective(body.artifact_id, body.plan_objective_id, body.objective_id)

    @app.post('/api/offerings/{oid}/unit-review/preview')
    def unit_review_preview(oid: str, body: UnitReviewBody):
        return ws().unit_review_preview(oid, body.assignment_ids)

    @app.post('/api/offerings/{oid}/unit-review')
    def draft_unit_review(oid: str, body: UnitReviewBody, request: Request):
        return once(
            request,
            f'unit-review:{oid}',
            lambda: ws().draft_unit_review(oid, body.assignment_ids, body.pattern_ids, body.title),
        )

    # ----- 局部修改, materials, deliverables, coursemaker -----

    @app.post('/api/artifacts/{aid}/revise')
    def revise_part(aid: str, body: ReviseBody, request: Request):
        return once(request, f'revise:{aid}', lambda: ws().revise_plan_part(aid, body.target, body.instruction))

    @app.put('/api/projects/{pid}/material-prefs')
    def material_prefs(pid: str, body: MaterialPrefs):
        return ws().set_material_prefs(pid, body.exclude, body.focus)

    @app.get('/api/projects/{pid}/deliverables/{kind}')
    def deliverable(pid: str, kind: str, fmt: str = 'html', round: int = 1, assignment_id: str | None = None):
        name, media, data = ws().deliverable(pid, kind, fmt, round, assignment_id or None)
        quoted = urllib.parse.quote(name)
        disposition = 'attachment' if fmt == 'docx' else 'inline'
        return Response(
            data, media_type=media, headers={'Content-Disposition': f"{disposition}; filename*=UTF-8''{quoted}"}
        )

    @app.post('/api/projects/{pid}/coursemaker-package')
    def coursemaker_package(pid: str):
        return ws().courseware_package(pid)

    @app.post('/api/projects/{pid}/courseware')
    def import_courseware(pid: str, body: CoursewareBody):
        return ws().import_courseware(pid, body.package_id, body.kind, body.files, body.external_run_id, body.note)

    @app.get('/api/migration/preview')
    def migration_preview():
        return ws().migration_preview()

    @app.post('/api/migration/map')
    def map_projects(body: MapBody, request: Request):
        return once(
            request,
            'map',
            lambda: ws().map_projects(
                body.project_ids,
                course_id=body.course_id,
                course=body.course,
                class_id=body.class_id,
                class_name=body.class_name,
            ),
        )

    @app.get('/api/projects/{pid}/stale')
    def stale(pid: str):
        return ws().stale(pid)

    @app.post('/api/stale/{mark_id}/resolve')
    def resolve(mark_id: int, body: ResolveBody):
        return ws().resolve_stale(mark_id, body.resolution)

    @app.get('/api/projects/{pid}/export')
    def export(pid: str):
        data = ws().export(pid)
        body = json.dumps(data, ensure_ascii=False, indent=1, default=str)
        return Response(
            body,
            media_type='application/json',
            headers={'Content-Disposition': f'attachment; filename="edu-export-{pid}.json"'},
        )

    @app.get('/')
    def index():
        # Version the asset URLs by content so a browser never runs an old script against a new API.
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        for name in ('app.js', 'app.css'):
            version = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
            html = html.replace(f'/static/{name}"', f'/static/{name}?v={version}"')
        return HTMLResponse(html, headers={'Cache-Control': 'no-cache'})

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        # There are no accounts: refuse anyone but this machine unless EDU_ALLOW_REMOTE is set.
        if not ws().settings.allow_remote and not is_local_client(request.client.host if request.client else None):
            return JSONResponse(
                {'detail': '只允许本机访问（需要远程访问请设置 EDU_ALLOW_REMOTE=1 并自行加鉴权）'}, status_code=403
            )
        return await call_next(request)

    @app.middleware('http')
    async def revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache'  # always revalidate (ETag), never serve stale code
        return response

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/api/{path:path}')
    def missing(path: str):
        raise HTTPException(404, f'接口不存在：/api/{path}')

    return app


app = create_app()
