"""Teacher-trial metrics (COURSE_WORKSPACE_DESIGN.md 13.2).

The five success criteria were fixed before any teacher uses the system. These tests pin how each number
is derived from records the system already keeps, so the trial does not depend on a teacher filling in a
form afterwards.
"""

import json
import random
import time

import pytest
from edu_fake import FakeLLM
from test_edu_workflow import HANDLERS

from edu_core import artifacts as A
from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    return Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(1))


def course_with_plan(ws, aliases=('甲', '乙')):
    crs = ws.create_course('高一数学·上学期', '数学', grade='高一')
    offering = ws.link_class(crs['id'], ws.create_class('高一（3）班', list(aliases))['id'])
    t = ws.start_task(crs['id'], 'prep', offering_id=offering['id'], knowledge_module='对数函数', lesson='第2课时')
    pid = t['project']['id']
    ws.add_material(pid, '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    plan = ws.draft_lesson_plan(pid)['lesson_plan']
    ws.confirm(plan['id'])
    return crs, offering, pid, plan


def task_of(ws, pid):
    with ws.store.read() as c:
        return dict(c.execute('SELECT * FROM tasks WHERE project_id=?', (pid,)).fetchone())


def set_start(ws, tid, at):
    with ws.store.tx() as c:
        c.execute('UPDATE tasks SET created_at=? WHERE id=?', (at, tid))


def set_confirm_at(ws, pid, artifact_id, at):
    with ws.store.tx() as c:
        c.execute(
            "UPDATE events SET at=? WHERE project_id=? AND kind='confirm' AND json_extract(payload,'$.artifact_id')=?",
            (at, pid, artifact_id),
        )


def row_for(report, tid):
    return next(t for t in report['tasks'] if t['id'] == tid)


def test_time_runs_from_task_start_to_the_first_confirmed_artifact(ws):
    _crs, _offering, pid, plan = course_with_plan(ws)
    tid = task_of(ws, pid)['id']
    set_start(ws, tid, 1000.0)
    set_confirm_at(ws, pid, plan['id'], 1120.0)
    with ws.store.tx() as c:
        # Keep only the run inserted here so the wait is exactly 90 s.
        c.execute('DELETE FROM runs WHERE task_id=?', (tid,))
        c.execute(
            "INSERT INTO runs(id,project_id,task_id,kind,status,started_at,finished_at) VALUES('r1',?,?, 'draft', 'succeeded', 1010, 1100)",
            (pid, tid),
        )
    row = row_for(ws.trial_report(), tid)
    assert row['usable_at'] == 1120.0 and row['total_seconds'] == 120.0
    assert row['wait_seconds'] == 90.0 and row['teacher_seconds'] == 30.0


def test_a_task_without_a_confirmed_result_reports_no_usable_time(ws):
    crs = ws.create_course('高一数学·上学期', '数学', grade='高一')
    started = ws.start_task(crs['id'], 'prep', knowledge_module='对数函数', lesson='第2课时')
    tid = task_of(ws, started['project']['id'])['id']
    row = row_for(ws.trial_report(), tid)
    assert row['usable_at'] is None and row['total_seconds'] is None


def test_edits_versions_and_regenerations_are_counted(ws):
    _crs, _offering, pid, plan = course_with_plan(ws)
    tid = task_of(ws, pid)['id']
    edited = ws.edit(plan['id'], {**plan['content'], 'notes': '加一道例题'})
    ws.confirm(edited['id'], change_kind='cosmetic')
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    [q] = ws.questions(pid)
    with ws.store.tx() as c:
        # What 换一道 stores: a new version in the same lineage that records where it came from.
        A.create(
            c,
            pid,
            'question',
            {**q['content'], 'regenerated_from': q['id'], 'instruction': '换一个情境'},
            origin='generated',
            lineage_id=q['lineage_id'],
        )
    row = row_for(ws.trial_report(), tid)
    assert row['edits'] >= 1 and row['versions'] >= 3
    assert row['regenerations'] >= 1


def test_a_plan_counts_as_used_only_when_its_lesson_is_marked_taught(ws):
    _crs, offering, pid, _plan = course_with_plan(ws)
    tid = task_of(ws, pid)['id']
    row = row_for(ws.trial_report(), tid)
    assert row['plan_used'] is False  # confirmed and scheduled, but not taught
    [session] = ws.sessions(offering['id'])
    ws.update_session(session['id'], status='taught')
    assert row_for(ws.trial_report(), tid)['plan_used'] is True


def test_questions_count_as_used_only_when_the_class_answered(ws):
    _crs, offering, pid, _plan = course_with_plan(ws)
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    tid = task_of(ws, pid)['id']
    assert row_for(ws.trial_report(), tid)['questions_used'] == 0
    a = ws.assign(pid, offering['id'])
    assert row_for(ws.trial_report(), tid)['questions_used'] == 0  # assigned, nobody answered yet
    ws.import_submissions(pid, f'甲,{q["content"]["no"]},定义域 x>1', assignment_id=a['id'])
    assert row_for(ws.trial_report(), tid)['questions_used'] == 1


def test_cross_class_errors_are_counted(ws):
    _crs, offering, pid, _plan = course_with_plan(ws)
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    a = ws.assign(pid, offering['id'])
    assert ws.trial_report()['summary']['cross_class_errors'] == 0
    with ws.store.tx() as c:
        A.create(
            c,
            pid,
            'submission',
            {
                'learner_id': 'someone-else',
                'learner_alias': '外班',
                'question_lineage': q['lineage_id'],
                'question_no': 1,
                'text': 'x',
            },
            origin='imported',
            status='confirmed',
            assignment_id=a['id'],
        )
    assert ws.trial_report()['summary']['cross_class_errors'] == 1


def test_repeated_entry_signals_are_reported(ws):
    _crs, _offering, pid, _plan = course_with_plan(ws)
    ws.add_learners(pid, ['丙'])
    ws.update_project(pid, lesson='第3课时')
    tid = task_of(ws, pid)['id']
    row = row_for(ws.trial_report(), tid)
    assert row['project_updates'] == 1 and row['roster_entry_calls'] == 1
    assert ws.trial_report()['summary']['repeated_entry']['project_updates'] == 1


def test_willingness_is_recorded_once_per_teacher(ws):
    assert ws.trial_report()['summary']['willingness'] == {'willing': 0, 'unwilling': 0, 'unanswered': 0}
    ws.record_trial_feedback(True, '愿意继续用')
    ws.record_trial_feedback(True, '仍然愿意')
    ws.record_trial_feedback(False, '改主意了')
    assert ws.trial_report()['summary']['willingness'] == {'willing': 0, 'unwilling': 1, 'unanswered': 0}


def test_report_can_be_scoped_to_one_course(ws):
    crs, _offering, pid, _plan = course_with_plan(ws)
    other = ws.create_course('高一物理·上学期', '物理', grade='高一')
    ws.start_task(other['id'], 'prep', knowledge_module='运动学', lesson='第1课时')
    assert len(ws.trial_report()['tasks']) == 2
    assert [t['id'] for t in ws.trial_report(course_id=crs['id'])['tasks']] == [task_of(ws, pid)['id']]


def test_feedback_records_who_and_when(ws):
    ws.record_trial_feedback(True, 'note')
    with ws.store.read() as c:
        r = c.execute("SELECT payload FROM events WHERE kind='trial_feedback' ORDER BY at DESC LIMIT 1").fetchone()
    payload = json.loads(r[0])
    assert payload['willing'] is True and payload['teacher'] == 'local' and payload['note'] == 'note'
    assert payload['at'] <= time.time()
