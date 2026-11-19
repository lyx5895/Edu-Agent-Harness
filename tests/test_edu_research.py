"""Run records: one teacher-started model operation, with every model call inside it."""

import random

import pytest
from edu_fake import FakeLLM
from test_edu_course import prep_and_practice, two_classes
from test_edu_workflow import HANDLERS

from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    handlers = dict(HANDLERS)
    handlers['regenerate_question'] = HANDLERS['generate_questions']
    return Workspace(Store(settings.db), FakeLLM(handlers), settings, rng=random.Random(1))


# ----- runs and research counts -----


def test_runs_separate_tasks_runs_and_model_calls(ws):
    crs, o3, _ = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    ws.regenerate_question(q['id'], '换一道')  # a retry: one more run, no new case
    with ws.store.read() as c:
        runs = [dict(r) for r in c.execute('SELECT * FROM runs WHERE project_id=?', (pid,))]
        untagged = c.execute("SELECT COUNT(*) FROM generations WHERE project_id=? AND run_id=''", (pid,)).fetchone()[0]
    assert sorted(r['kind'] for r in runs) == ['draft_lesson_plan', 'generate_questions', 'regenerate_question']
    assert all(r['status'] == 'succeeded' for r in runs) and untagged == 0  # self-checks in worker threads too
    counts = ws.export(pid)['manifest']['counts']
    assert counts['teacher_tasks'] == 1 and counts['runs'] == 3 and counts['model_calls'] > counts['runs']
    assert counts['contributors'] is None  # no accounts: the number of teachers is not guessed


def test_failed_run_is_recorded(ws):
    p = ws.create_project('x', '数学', knowledge_module='对数函数')
    ws.llm.handlers['draft_lesson_plan'] = lambda r, payload: (_ for _ in ()).throw(RuntimeError('网关超时'))
    with pytest.raises(RuntimeError):
        ws.draft_lesson_plan(p['id'])
    with ws.store.read() as c:
        r = c.execute('SELECT status, error FROM runs WHERE project_id=?', (p['id'],)).fetchone()
    assert r['status'] == 'failed' and '网关超时' in r['error']
