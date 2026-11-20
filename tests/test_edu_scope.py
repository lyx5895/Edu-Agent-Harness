"""Scope: one explicit answer to "which world is this student work in?".

A class assignment uses the question and rubric versions it was given and its roster snapshot; the
project's own unbound work follows the latest confirmed versions and the project's learners. The
stage methods used to re-derive this from a ``(project, round, assignment)`` triple; these tests pin
the semantics of the object they now share.
"""

import random

import pytest
from edu_fake import FakeLLM
from test_edu_workflow import HANDLERS

from edu_core import artifacts as A
from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    return Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(1))


def course_with_one_question(ws, aliases=('甲', '乙')):
    crs = ws.create_course('高一数学·上学期', '数学', grade='高一')
    cls = ws.create_class('高一（3）班', list(aliases))
    offering = ws.link_class(crs['id'], cls['id'])
    t = ws.start_task(crs['id'], 'prep', offering_id=offering['id'], knowledge_module='对数函数', lesson='第2课时')
    pid = t['project']['id']
    ws.add_material(pid, '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    ws.confirm(ws.draft_lesson_plan(pid)['lesson_plan']['id'])
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    return crs, cls, offering, pid, q


def test_unbound_scope_keeps_its_round_and_needs_a_project(ws):
    with ws.store.read() as c:
        with pytest.raises(ValueError):
            ws.scope(c, None, None, None)
        s = ws.scope(c, 'p1', 2, '')
        assert (s.project_id, s.round, s.assignment_id) == ('p1', 2, '')
        assert not s.is_assignment and s.kind == 'project'
        assert ws.scope(c, 'p1', None, '').round == 1


def test_assignment_scope_takes_its_round_and_offering_from_the_record(ws):
    _crs, _cls, offering, pid, _q = course_with_one_question(ws)
    a = ws.assign(pid, offering['id'])
    with ws.store.read() as c:
        s = ws.scope(c, pid, 99, a['id'])  # the assignment's round wins over the argument
        assert (s.round, s.assignment_id, s.offering_id) == (1, a['id'], offering['id'])
        assert s.is_assignment and s.kind == 'assignment'
        with pytest.raises(Conflict, match='不属于当前项目'):
            ws.scope(c, 'another-project', None, a['id'])


def test_assignment_questions_and_rubrics_are_pinned(ws):
    _crs, _cls, offering, pid, q = course_with_one_question(ws)
    a = ws.assign(pid, offering['id'])
    no, lin = q['content']['no'], q['lineage_id']
    edited = ws.edit(q['id'], {**q['content'], 'stem': q['content']['stem'] + '（改）'})
    ws.confirm(edited['id'])
    rubric = q['rubric']['content']
    new_rubric = ws.edit(
        q['rubric']['id'], {**rubric, 'points': [{**rubric['points'][0], 'score': 3}, *rubric['points'][1:]]}
    )
    ws.confirm(new_rubric['id'])
    with ws.store.read() as c:
        pinned, live = ws.scope(c, pid, 1, a['id']), ws.scope(c, pid, 1, '')
        assert pinned.questions(c)[no]['id'] == q['id'] and live.questions(c)[no]['id'] == edited['id']
        assert pinned.rubric(c, lin)['id'] == q['rubric']['id'] and live.rubric(c, lin)['id'] == new_rubric['id']
        assert pinned.pins(c)[lin] == {
            'question': q['id'],
            'rubric': q['rubric']['id'],
            'rubric_lineage': q['rubric']['lineage_id'],
            'no': no,
        }


def test_roster_snapshot_survives_a_member_leaving(ws):
    _crs, cls, offering, pid, _q = course_with_one_question(ws)
    a = ws.assign(pid, offering['id'])
    left = next(m for m in ws.class_detail(cls['id'])['members'] if m['alias'] == '乙')
    ws.remove_member(cls['id'], left['id'])
    with ws.store.read() as c:
        assert set(ws.scope(c, pid, 1, a['id']).roster(c)) == {'甲', '乙'}
        assert set(ws.scope(c, pid, 1, '').roster(c)) == set()  # the project's own roster is empty
    with ws.store.tx() as c:
        assert ws.scope(c, pid, 1, '').add_learner(c, '丙')
    with ws.store.read() as c:
        assert set(ws.scope(c, pid, 1, '').roster(c)) == {'丙'}


def test_coverage_is_the_same_shape_in_both_kinds(ws):
    _crs, _cls, offering, pid, q = course_with_one_question(ws)
    a = ws.assign(pid, offering['id'])
    no = q['content']['no']
    ws.import_submissions(pid, f'甲,{no},定义域 x>1', assignment_id=a['id'])
    ws.add_learners(pid, ['甲', '乙'])
    ws.import_submissions(pid, f'甲,{no},定义域 x>1')
    for aid, label in ((a['id'], 'assignment'), ('', 'project')):
        with ws.store.read() as c:
            s = ws.scope(c, pid, 1, aid)
            subs = A.current(c, pid, 'submission', round_=1, assignment=s.assignment_id)
            cov = s.coverage(c, subs)
            assert (cov['expected'], cov['submitted'], cov['missing']) == (2, 1, 1), label
            assert [m['learner_alias'] for m in cov['missing_answers']] == ['乙'], label
            assert cov['missing_answers'][0]['question_no'] == no


def test_stage_methods_read_the_same_scope(ws):
    _crs, _cls, offering, pid, q = course_with_one_question(ws)
    a = ws.assign(pid, offering['id'])
    ws.import_submissions(pid, f'甲,{q["content"]["no"]},定义域 x>1', assignment_id=a['id'])
    ws.grade_round(pid, assignment_id=a['id'])
    ws.confirm_gradings(pid, assignment_id=a['id'])
    view = ws.assignment(a['id'])
    seen = ws.analyze(pid, assignment_id=a['id'], partial=True)['analysis']['content']['coverage']
    assert view['expected'] == seen['expected'] == 2
    assert view['missing'] == seen['missing'] == 1
    assert view['missing_answers'] == seen['missing_answers']
