"""Run records, course objectives and the unit review."""

import random

import pytest
from edu_fake import FakeLLM
from test_edu_course import GOOD, MISSED, graded, prep_and_practice, two_classes
from test_edu_workflow import HANDLERS

from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    handlers = dict(HANDLERS)
    handlers['regenerate_question'] = HANDLERS['generate_questions']
    return Workspace(Store(settings.db), FakeLLM(handlers), settings, rng=random.Random(1))


def analysed_class(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    a3 = ws.assign(pid, o3['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    out = ws.analyze(pid, assignment_id=a3['id'])
    ws.confirm(out['patterns'][0]['id'])
    ws.confirm(out['analysis']['id'])
    return crs, o3, o5, pid, q, a3


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


# ----- course objectives -----


def test_course_objectives_are_stable_and_versioned(ws):
    crs, _o3, o5, pid, _q, _a3 = analysed_class(ws)
    [obj] = ws.course_objectives(crs['id'])
    assert obj['code'] == 'G1' and obj['plans'] == 1 and obj['version'] == 1
    other = ws.start_task(crs['id'], 'prep', offering_id=o5['id'], knowledge_module='对数函数')['project']['id']
    ws.confirm(ws.draft_lesson_plan(other)['lesson_plan']['id'])  # same wording: same course objective
    assert [o['plans'] for o in ws.course_objectives(crs['id'])] == [2]

    plan = ws.lesson_plans(pid)[0]
    changed = ws.edit(
        plan['id'], {'objectives': [{**plan['content']['objectives'][0], 'text': '解对数不等式时先检查定义域'}]}
    )
    ws.confirm(changed['id'], change_kind='substantive')
    [obj] = ws.course_objectives(crs['id'])
    assert obj['version'] == 2 and obj['needs_review']  # wording changed: mapped questions need a look
    ws.mark_objective_reviewed(obj['id'])
    view = ws.objective_view(obj['id'])
    assert [v['version'] for v in view['versions']] == [1, 2] and len(view['plans']) == 2
    assert view['questions'][0]['no'] == 1
    cls = {c['class_name']: c for c in view['classes']}
    ev = cls['高一（3）班']['assignments'][0]['evidence']
    assert ev['rate'] == 0.5 and ev['help'].startswith('未知')  # imported answers: help unknown

    ws.relink_objective(changed['id'], 'O1', '')  # the teacher says it is a different objective
    assert len(ws.course_objectives(crs['id'])) == 2


# ----- unit review -----


def test_unit_review_over_two_assignments_of_one_class(ws):
    _crs, o3, o5, pid, _q, a3 = analysed_class(ws)
    ws.llm.handlers['practice'] = HANDLERS['practice']
    prac = ws.practice(pid, assignment_id=a3['id'])
    for pq in prac['questions']:
        ws.confirm(pq['id'])
    b3 = ws.assign(pid, o3['id'], round_=prac['round'], parent_id=a3['id'])
    rows = ws.unit_review_preview(o3['id'], [a3['id'], b3['id']])
    assert [r['open_items'] for r in rows] == [0, 0] and rows[0]['patterns'] and rows[1]['received'] == 0
    out = ws.draft_unit_review(o3['id'], [a3['id'], b3['id']])
    plan = out['review_plan']
    assert plan['content']['scope'] == 'unit' and len(plan['content']['assignments']) == 2
    assert out['project']['offering_id'] == o3['id'] and '所选的几份作业' in out['context_note']
    a5 = ws.assign(pid, o5['id'])
    with pytest.raises(Conflict):
        ws.unit_review_preview(o3['id'], [a3['id'], a5['id']])  # never across classes
