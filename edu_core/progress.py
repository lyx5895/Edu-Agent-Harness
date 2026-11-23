"""Live progress of running model operations, so the page can say what is happening while a teacher waits.

Kept in this process's memory only (one uvicorn worker): it describes work in flight, nothing to persist. Each run
(see workflow/runs.py) registers on start and is removed when it ends; the operation reports its current step
(e.g. "独立解题与审核 2/3") from wherever it is, the current run being found through ``CURRENT_RUN``.
"""

from __future__ import annotations

import threading
import time

from edu_core.storage import CURRENT_RUN

LABELS = {
    'draft_lesson_plan': '生成教案',
    'revise_plan': '修改教案',
    'generate_questions': '出题',
    'generate_checks': '自检与替代版本',
    'regenerate_question': '换一道题',
    'revise_question': '按意见修改题目',
    'import_paper': '整理试卷',
    'transcribe': '识别作答照片',
    'grade': '批改作答',
    'analyze': '汇总学情',
    'draft_review': '生成讲评稿',
    'practice': '生成再练题',
    'explain': '生成讲解',
    'unit_review': '单元讲评',
    'check_plan': 'AI 看教案',
}

_lock = threading.Lock()
_live: dict[str, dict] = {}


def begin(run_id, project_id, kind):
    with _lock:
        _live[run_id] = {
            'run_id': run_id,
            'project_id': project_id or '',
            'kind': kind,
            'label': LABELS.get(kind, kind),
            'step': LABELS.get(kind, kind),
            'done': None,
            'total': None,
            'started_at': time.time(),
        }


def end(run_id):
    with _lock:
        _live.pop(run_id, None)


def step(label, done=None, total=None):
    """The current run is now at ``label`` (optionally ``done`` of ``total`` items)."""
    rid = CURRENT_RUN.get()
    with _lock:
        if rid in _live:
            _live[rid].update(step=label, done=done, total=total)


def grow(label, n):
    """Add ``n`` items to the step ``label``: parallel parts (the two A/B versions) each add their own items to one
    shared count instead of overwriting each other's."""
    rid = CURRENT_RUN.get()
    with _lock:
        live = _live.get(rid)
        if not live:
            return
        if live['step'] != label or live['total'] is None:
            live.update(step=label, done=0, total=n)
        else:
            live['total'] += n


def tick():
    """One more item of the current step is finished (called from worker threads)."""
    rid = CURRENT_RUN.get()
    with _lock:
        live = _live.get(rid)
        if live and live['done'] is not None:
            live['done'] += 1


def running(project_id):
    with _lock:
        return [dict(x) for x in _live.values() if x['project_id'] == project_id]
