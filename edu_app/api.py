"""HTTP API and static teacher workspace for edu_core.

Local single-user tool for now: no accounts (bind to 127.0.0.1). Model calls are synchronous; FastAPI
runs these handlers in its thread pool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    subject: str | None = None
    grade: str | None = None
    notes: str | None = Field(default=None, max_length=2000)


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


class ManualQuestion(BaseModel):
    question: dict[str, Any]
    rubric: dict[str, Any]
    round: int = 1


class ChoiceBody(BaseModel):
    group: str
    verdict: str
    reason: str = Field(default='', max_length=500)


class LearnersBody(BaseModel):
    aliases: list[str] = Field(max_length=500)


class SubmissionsBody(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)
    round: int = 1
    add_missing: bool = False


class RoundBody(BaseModel):
    round: int = 1
    regrade: bool = False


class OverrideBody(BaseModel):
    points: list[dict[str, Any]]
    note: str = Field(default='', max_length=1000)


class ConfirmGradings(BaseModel):
    round: int = 1
    ids: list[str] | None = None


class PracticeBody(BaseModel):
    pattern_ids: list[str] | None = None
    count: int = Field(default=1, ge=1, le=3)


class CompareBody(BaseModel):
    round_a: int = 1
    round_b: int = 2


class ResolveBody(BaseModel):
    resolution: str


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


def create_app(workspace: Workspace | None = None) -> FastAPI:
    app = FastAPI(title='Edu-Agent 教师端', version='0.11.0-dev')
    state = {'ws': workspace}

    def ws() -> Workspace:
        if state['ws'] is None:
            state['ws'] = default_workspace()
        return state['ws']

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
            'models': w.settings.models,
            'model_ready': w.llm is not None,
        }

    @app.get('/api/projects')
    def projects():
        return ws().projects()

    @app.post('/api/projects')
    def create_project(body: NewProject):
        return ws().create_project(body.title, body.subject, body.grade, body.notes)

    @app.get('/api/projects/{pid}')
    def overview(pid: str):
        return ws().overview(pid)

    @app.patch('/api/projects/{pid}')
    def update_project(pid: str, body: ProjectUpdate):
        return ws().update_project(pid, **body.model_dump())

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

    @app.post('/api/projects/{pid}/objectives/draft')
    def draft_objectives(pid: str):
        return ws().draft_objectives(pid)

    @app.post('/api/projects/{pid}/objectives')
    def manual_objective(pid: str, body: ContentBody):
        return ws().create_manual(pid, 'objective', body.content)

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
    def generate(pid: str, body: GenerateBody):
        return ws().generate_questions(
            pid, types=body.types, count=body.count, difficulty=body.difficulty, notes=body.notes, ab=body.ab
        )

    @app.post('/api/projects/{pid}/questions')
    def manual_question(pid: str, body: ManualQuestion):
        return ws().create_question(pid, body.question, body.rubric, round_=body.round)

    @app.get('/api/projects/{pid}/questions')
    def questions(pid: str, round: int | None = None):
        return ws().questions(pid, round)

    @app.post('/api/projects/{pid}/choices')
    def choose(pid: str, body: ChoiceBody):
        return ws().choose(pid, body.group, body.verdict, body.reason)

    @app.post('/api/projects/{pid}/import-paper')
    def import_paper(pid: str, body: TextBody):
        return ws().import_paper(pid, body.text, body.filename)

    @app.get('/api/projects/{pid}/learners')
    def learners(pid: str):
        return ws().learners(pid)

    @app.post('/api/projects/{pid}/learners')
    def add_learners(pid: str, body: LearnersBody):
        return ws().add_learners(pid, body.aliases)

    @app.post('/api/projects/{pid}/submissions')
    def import_submissions(pid: str, body: SubmissionsBody):
        return ws().import_submissions(pid, body.csv, body.round, body.add_missing)

    @app.get('/api/projects/{pid}/submissions')
    def submissions(pid: str, round: int = 1):
        return ws().submissions(pid, round)

    @app.post('/api/projects/{pid}/grade')
    def grade(pid: str, body: RoundBody):
        return ws().grade_round(pid, body.round, body.regrade)

    @app.get('/api/projects/{pid}/gradings')
    def gradings(pid: str, round: int = 1):
        return ws().gradings(pid, round)

    @app.get('/api/projects/{pid}/review-queue')
    def queue(pid: str, round: int = 1):
        return ws().review_queue(pid, round)

    @app.post('/api/gradings/{gid}/override')
    def override(gid: str, body: OverrideBody):
        return ws().override(gid, body.points, body.note)

    @app.post('/api/projects/{pid}/gradings/confirm')
    def confirm_gradings(pid: str, body: ConfirmGradings):
        return ws().confirm_gradings(pid, body.round, body.ids)

    @app.post('/api/projects/{pid}/analyze')
    def analyze(pid: str, body: RoundBody):
        return ws().analyze(pid, body.round)

    @app.get('/api/projects/{pid}/analyses')
    def analyses(pid: str):
        return ws().analyses(pid)

    @app.get('/api/projects/{pid}/patterns')
    def patterns(pid: str):
        return ws().patterns(pid)

    @app.post('/api/projects/{pid}/practice')
    def practice(pid: str, body: PracticeBody):
        return ws().practice(pid, body.pattern_ids, body.count)

    @app.post('/api/projects/{pid}/compare')
    def compare(pid: str, body: CompareBody):
        return ws().compare(pid, body.round_a, body.round_b)

    @app.get('/api/projects/{pid}/comparisons')
    def comparisons(pid: str):
        from edu_core import artifacts as A

        with ws().store.read() as c:
            return A.current(c, pid, 'comparison')

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
