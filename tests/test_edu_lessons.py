"""一节课的闭环 (LESSON_LOOP_DESIGN.md): one class per course, background filled once, a lesson's student work is its
own (latest confirmed versions apply; a substantive change asks for an update), 进入下一课时 stays in the course."""

import json
import random

import pytest
from edu_fake import FakeLLM
from test_edu_workflow import HANDLERS

from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace

GOOD = '定义域 x>1，又 x-1<8，所以 1<x<9'
MISSED = '由单调性 x-1<8，所以 x<9'


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    return Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(1))


def course(ws):
    return ws.setup_course('高一数学', '数学', grade='高一', class_name='高一（3）班', roster=['甲', '乙'])


def planned_with_question(ws, crs):
    pid = ws.new_lesson(crs['id'], '对数函数', '第 2 课时')['project']['id']
    ws.add_material(pid, '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    ws.confirm(ws.draft_lesson_plan(pid)['lesson_plan']['id'])
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    return pid, q


def test_setup_fills_the_background_once_and_a_lesson_gets_the_roster(ws):
    crs = course(ws)
    assert crs['class']['name'] == '高一（3）班' and [m['alias'] for m in crs['members']] == ['甲', '乙']
    started = ws.new_lesson(crs['id'], '对数函数', '第 1 课时', start='grading')
    assert started['tab'] == 'grading'
    pid = started['project']['id']
    assert [x['alias'] for x in ws.learners(pid)] == ['甲', '乙']
    assert started['project']['grade'] == '高一' and started['project']['subject'] == 'math'
    with pytest.raises(Conflict):
        ws.new_lesson(crs['id'], '  ')  # a lesson is named by its topic


def test_a_learner_added_in_a_lesson_joins_the_class_for_the_next_lessons(ws):
    crs = course(ws)
    pid = ws.new_lesson(crs['id'], '对数函数', '第 1 课时')['project']['id']
    ws.add_learners(pid, ['丙'])
    assert '丙' in [m['alias'] for m in ws.lesson_course(crs['id'])['members']]
    later = ws.new_lesson(crs['id'], '对数函数', '第 2 课时')['project']['id']
    assert [x['alias'] for x in ws.learners(later)] == ['甲', '乙', '丙']


def test_lesson_work_is_unbound_and_a_rubric_change_asks_for_an_update(ws):
    crs = course(ws)
    pid, _q = planned_with_question(ws, crs)
    ws.import_submissions(pid, f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.grade_round(pid)
    ov = ws.overview(pid, assignment_id='')
    assert ov['assignment_id'] == '' and ov['assignments'] == []
    rubric = ws.questions(pid)[0]['rubric']
    pts = rubric['content']['points']
    new = ws.edit(rubric['id'], {'points': [{**pts[0], 'score': pts[0]['score'] + 1}, *pts[1:]]})
    ws.confirm(new['id'], change_kind='substantive')
    stale = ws.overview(pid, assignment_id='')['stale']
    assert {m['type'] for m in stale} == {'grading'}  # the gradings built on the old rubric are marked


def test_lessons_list_says_where_each_loop_stands(ws):
    crs = course(ws)
    pid, _ = planned_with_question(ws, crs)
    fresh = ws.new_lesson(crs['id'], '对数函数', '第 3 课时', start='explain')['project']['id']
    rows = {r['id']: r for r in ws.lessons(crs['id'])}
    assert rows[pid]['stages']['prep'] and rows[pid]['stages']['questions'] and not rows[pid]['stages']['grading']
    assert rows[pid]['next']['tab'] == 'grading' and rows[pid]['title'] == '对数函数｜第 2 课时'
    assert rows[fresh]['next']['tab'] == 'review' and rows[fresh]['state'] == '刚开始'


def test_next_lesson_stays_in_the_course_with_the_roster(ws):
    crs = course(ws)
    pid, _ = planned_with_question(ws, crs)
    ws.import_submissions(pid, f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    out = ws.analyze(pid, partial=True)
    for x in (out['analysis'], *out['patterns']):
        ws.confirm(x['id'])
    nxt = ws.next_lesson(pid)
    assert nxt['course_id'] == crs['id'] and nxt['carryover']['from_project'] == pid
    assert [x['alias'] for x in ws.learners(nxt['id'])] == ['甲', '乙']
    assert nxt['id'] in [r['id'] for r in ws.lessons(crs['id'])]


def test_a_new_lesson_reads_no_learning_status_from_other_lessons(ws):
    """Only 进入下一课时 carries 学情; a lesson started on its own does not read the class's other lessons."""
    crs = course(ws)
    pid, _ = planned_with_question(ws, crs)
    # Lessons made before this version kept their work in class assignments: that is what 本班学情 reads.
    aid = ws.assign(pid, crs['class']['offering_id'])['id']
    ws.import_submissions(pid, f'甲,1,{GOOD}\n乙,1,{MISSED}', assignment_id=aid)
    ws.grade_round(pid, assignment_id=aid)
    ws.confirm_gradings(pid, assignment_id=aid)
    out = ws.analyze(pid, assignment_id=aid)
    for x in (out['analysis'], *out['patterns']):
        ws.confirm(x['id'])
    other = ws.new_lesson(crs['id'], '对数函数', '第 5 课时')['project']['id']
    plan = ws.draft_lesson_plan(other)
    assert '学情' not in plan['context_note']
    sent = json.dumps(
        next(x for x in reversed(ws.llm.calls) if x['task'] == 'draft_lesson_plan')['user'], ensure_ascii=False
    )
    assert '本班学情' not in sent and '上一课时学情' not in sent and '求出解' not in sent
