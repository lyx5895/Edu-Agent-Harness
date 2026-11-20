"""Course workspace (COURSE_WORKSPACE_DESIGN.md 13.1, first version): courses, classes, tasks, sessions,
class assignments with pinned versions, rubric correction for one class, coverage, write-back checks,
idempotency and mapping old projects."""

import random
from pathlib import Path

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


def two_classes(ws):
    crs = ws.create_course('高一数学·上学期', '数学', grade='高一')
    c3 = ws.create_class('高一（3）班', ['甲', '乙'])
    c5 = ws.create_class('高一（5）班', ['丙', '丁'])
    return crs, ws.link_class(crs['id'], c3['id']), ws.link_class(crs['id'], c5['id'])


def prep_and_practice(ws, crs, offering):
    t = ws.start_task(crs['id'], 'prep', offering_id=offering['id'], knowledge_module='对数函数', lesson='第2课时')
    pid = t['project']['id']
    ws.add_material(pid, '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    plan = ws.draft_lesson_plan(pid)
    assert ws.sessions(offering['id']) == []  # a draft alone never creates a session
    ws.confirm(plan['lesson_plan']['id'])
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    return pid, q


def graded(ws, pid, aid, rows):
    ws.import_submissions(pid, rows, assignment_id=aid)
    ws.grade_round(pid, assignment_id=aid)
    return ws.gradings(pid, assignment_id=aid)


def test_first_use_needs_only_course_name_and_subject(ws):
    crs = ws.create_course('机器人社团', '机器人')  # no subject pack: the general flow
    assert crs['subject'] == '机器人' and crs['subject_name'] == '机器人'
    started = ws.start_task(crs['id'], 'prep', knowledge_module='传感器')
    p = started['project']
    assert p['course_id'] == crs['id'] and p['subject'] == '机器人' and not p['has_curriculum']
    assert started['tab'] == 'prep' and started['task']['kind_label'] == '备课'
    assert ws.home()['tasks'][0]['project_id'] == p['id']


def test_plan_adoption_creates_one_session_and_next_lesson_is_a_suggestion(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    [s] = ws.sessions(o3['id'])
    assert s['status'] == 'planned' and s['title'] == '对数函数｜第2课时' and s['plan_version'] == 1

    plan = ws.lesson_plans(pid)[0]
    edited = ws.edit(plan['id'], {'notes': '加一道例题'})
    ws.confirm(edited['id'], change_kind='cosmetic')
    [s] = ws.sessions(o3['id'])  # still one session, now with the adopted version
    assert s['plan_version'] == 2

    nxt = ws.suggest_next_lesson(o3['id'])
    assert nxt['lesson'] == '第 3 课时' and nxt['knowledge_module'] == '对数函数'
    assert ws.sessions(o3['id'])[0]['status'] == 'planned'  # suggesting never marks the last lesson taught

    ws.update_session(s['id'], planned_date='2000-01-01')
    assert ws.sessions(o3['id'])[0]['overdue']
    assert any(a['kind'] == 'session' for a in ws.home(crs['id'])['attention'])
    ws.update_session(s['id'], status='taught')
    assert ws.course(crs['id'])['offerings'][0]['progress'] == '对数函数｜第2课时'


def test_same_practice_for_two_classes_keeps_data_and_versions_apart(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    a3 = ws.assign(pid, o3['id'])
    a5 = ws.assign(pid, o5['id'])
    assert a3['id'] != a5['id'] and a3['roster'] == 2 and a3['status_label'] == '待收集'

    out = ws.import_submissions(pid, f'甲,1,{GOOD}\n丙,1,{GOOD}', assignment_id=a3['id'])
    assert out['created'] == 1 and out['unmatched_learners'] == ['丙']  # 丙 is in class 5, not class 3
    ws.import_submissions(pid, f'丙,1,{MISSED}', assignment_id=a5['id'])
    assert [s['content']['learner_alias'] for s in ws.submissions(pid, assignment_id=a3['id'])] == ['甲']
    assert [s['content']['learner_alias'] for s in ws.submissions(pid, assignment_id=a5['id'])] == ['丙']
    assert ws.submissions(pid) == []  # the project's unbound work is separate too

    ws.grade_round(pid, assignment_id=a3['id'])
    ws.grade_round(pid, assignment_id=a5['id'])

    # A newer public version of the rubric does not change what was assigned.
    rubric = ws.questions(pid)[0]['rubric']
    new = ws.edit(
        rubric['id'], {'points': [*rubric['content']['points'], {'id': 'P3', 'description': '书写规范', 'score': 1}]}
    )
    ws.confirm(new['id'], change_kind='substantive')
    assert not [m for m in ws.stale(pid) if m['type'] == 'grading']
    assert ws.assignment(a3['id'])['updates'] == [{'question_lineage': q['lineage_id'], 'no': 1, 'changed': ['rubric']}]
    assert ws.grade_round(pid, assignment_id=a3['id'])['graded'] == 0
    assert len(ws.gradings(pid, assignment_id=a3['id'])[0]['content']['points']) == 2

    # Re-pinning is explicit and regrades only that class.
    assert ws.repin(a5['id'], q['lineage_id'])['regrade'] == 1
    assert ws.grade_round(pid, assignment_id=a5['id'])['graded'] == 1
    assert len(ws.gradings(pid, assignment_id=a5['id'])[0]['content']['points']) == 3
    assert len(ws.gradings(pid, assignment_id=a3['id'])[0]['content']['points']) == 2


def test_rubric_correction_stays_in_one_class_and_lists_the_others(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    a3, a5 = ws.assign(pid, o3['id']), ws.assign(pid, o5['id'])
    old3 = graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    graded(ws, pid, a5['id'], f'丙,1,{GOOD}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])

    impact = ws.correction_impact(a3['id'], q['lineage_id'])
    assert impact['gradings'] == 2 and [o['class_name'] for o in impact['other_assignments']] == ['高一（5）班']
    rubric = impact['rubric']['content']
    out = ws.correct_rubric(
        a3['id'],
        q['lineage_id'],
        {'points': [{**rubric['points'][0], 'score': 3}, rubric['points'][1]]},
        note='定义域更重要',
    )
    assert out['regrade'] == 2 and [o['class_name'] for o in out['other_assignments']] == ['高一（5）班']
    assert {m['assignment_id'] for m in ws.stale(pid) if m['type'] == 'grading'} == {a3['id']}

    with pytest.raises(Conflict) as err:
        ws.analyze(pid, assignment_id=a3['id'])  # its gradings must be redone before a complete analysis
    assert '依据已变化 2' in str(err.value)
    assert ws.grade_round(pid, assignment_id=a3['id'])['graded'] == 2
    new3 = {g['content']['learner_alias']: g for g in ws.gradings(pid, assignment_id=a3['id'])}
    assert new3['甲']['content']['max_total'] == 5 and new3['甲']['version'] == 2
    assert new3['甲']['confirmed_version'] == 1  # the old valid result stays until the new one is confirmed
    assert {g['lineage_id'] for g in old3} == {g['lineage_id'] for g in new3.values()}
    assert ws.gradings(pid, assignment_id=a5['id'])[0]['content']['max_total'] == 4  # class 5 untouched


def test_partial_analysis_is_labelled_and_complete_needs_every_grading(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    a3 = ws.assign(pid, o3['id'])
    gs = graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    missed = next(g for g in gs if g['content']['learner_alias'] == '乙')
    ws.confirm_gradings(pid, assignment_id=a3['id'], ids=[missed['id']])

    with pytest.raises(Conflict) as err:
        ws.analyze(pid, assignment_id=a3['id'])
    assert '有效批改 1/2 份' in str(err.value)
    out = ws.analyze(pid, assignment_id=a3['id'], partial=True)
    cov = out['analysis']['content']['coverage']
    assert out['analysis']['content']['partial'] and cov['valid'] == 1 and cov['pending'] == 1 and cov['roster'] == 2

    ws.confirm_gradings(pid, assignment_id=a3['id'])
    full = ws.analyze(pid, assignment_id=a3['id'])
    assert not full['analysis']['content']['partial'] and full['analysis']['assignment_id'] == a3['id']


def test_class_status_reaches_the_next_plan_of_that_class_only(ws):
    crs, o3, o5 = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    a3 = ws.assign(pid, o3['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    out = ws.analyze(pid, assignment_id=a3['id'])
    assert ws.class_status(o3['id']) is None  # nothing confirmed yet
    ws.confirm(out['patterns'][0]['id'])
    ws.confirm(out['analysis']['id'])

    status = ws.class_status(o3['id'])
    assert status['patterns'][0]['name'] == '解对数不等式时遗漏定义域' and status['analysis_ids'] == [
        out['analysis']['id']
    ]
    assert ws.class_status(o5['id']) is None

    nxt = ws.start_task(crs['id'], 'prep', offering_id=o3['id'], knowledge_module='对数函数', lesson='第3课时')
    plan = ws.draft_lesson_plan(nxt['project']['id'])
    assert '本班学情' in plan['context_note']
    sent = ws.llm.calls[-1]['user']['本班学情']
    assert sent['班级与进度'] == '高一（3）班' and sent['问题模式'][0]['涉及人数'] == '1/2'

    other = ws.start_task(crs['id'], 'prep', offering_id=o5['id'], knowledge_module='对数函数')
    assert '本班学情' not in ws.draft_lesson_plan(other['project']['id'])['context_note']


def test_result_computed_on_a_changed_basis_is_not_adopted(ws):
    p = ws.create_project('对数函数', '数学', knowledge_module='对数函数')
    pid = p['id']
    ws.confirm(ws.draft_lesson_plan(pid)['lesson_plan']['id'])
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    ws.add_learners(pid, ['甲'])
    ws.import_submissions(pid, f'甲,1,{GOOD}')
    grade = ws.llm.handlers['grade']

    def grade_while_teacher_edits(role, payload):
        rubric = ws.questions(pid)[0]['rubric']
        pts = rubric['content']['points']
        ws.confirm(ws.edit(rubric['id'], {'points': [*pts, {'id': 'P3', 'description': '书写', 'score': 1}]})['id'])
        ws.llm.handlers['grade'] = grade
        return grade(role, payload)

    ws.llm.handlers['grade'] = grade_while_teacher_edits
    assert ws.grade_round(pid)['graded'] == 1
    [g] = ws.gradings(pid)
    assert g['stale']  # saved as based on the old rubric
    assert ws.confirm_gradings(pid) == {'confirmed': 0, 'held': 1}
    with pytest.raises(Conflict):
        ws.confirm(g['id'])
    ws.grade_round(pid)
    [g] = ws.gradings(pid)
    assert not g['stale'] and g['content']['max_total'] == 5


def test_idempotency_key_returns_the_first_result(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    first = ws.once('k-1', 'assign', lambda: ws.assign(pid, o3['id']))
    again = ws.once('k-1', 'assign', lambda: ws.assign(pid, o3['id']))
    assert first['id'] == again['id'] and len(ws.assignments(pid)) == 1

    def fails():
        raise Conflict('网络中断')

    with pytest.raises(Conflict):
        ws.once('k-2', 'assign', fails)
    ws.once('k-2', 'assign', lambda: ws.assign(pid, o3['id']))  # a failed run frees its key
    assert len(ws.assignments(pid)) == 2


def test_course_materials_are_shared_and_truncation_is_reported(ws):
    crs = ws.create_course('高一数学', '数学')
    ws.add_material(None, '课程公共资料：' + '对数' * 4500, filename='教材第四章', course_id=crs['id'])
    ws.add_material(None, '单元目录', filename='目录', course_id=crs['id'])
    t = ws.start_task(crs['id'], 'prep', knowledge_module='对数函数')
    pid = t['project']['id']
    assert [m['scope'] for m in ws.materials(pid)] == ['course', 'course']
    note = ws.draft_lesson_plan(pid)['context_note']
    assert '材料 2 份（1 份截断，1 份未发送（超出长度））' in note


def test_old_project_is_mapped_into_a_course_without_changing_records(ws):
    p = ws.create_project('高一数学｜对数函数｜第2课时', '数学', knowledge_module='对数函数')
    pid = p['id']
    ws.confirm(ws.draft_lesson_plan(pid)['lesson_plan']['id'])
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.confirm(q['id'])
    ws.add_learners(pid, ['甲', '乙'])
    ws.import_submissions(pid, f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    before = {g['id']: g['content']['total'] for g in ws.gradings(pid)}

    [group] = ws.migration_preview()['groups']
    assert group['projects'][0]['id'] == pid and group['projects'][0]['rounds_with_student_work'] == [1]
    assert group['suggested_course'] == {'title': '高一数学·对数函数', 'subject': 'math', 'grade': '高一'}
    assert ws.home()['legacy_projects'] == 1

    out = ws.map_projects([pid], course={'title': '高一数学·上学期'}, class_name='高一（3）班')
    assert out['checked']['artifacts'] > 0 and Path(out['backup']).exists()
    [aid] = out['assignments']
    assert ws.gradings(pid) == []  # the round's student work now belongs to the class assignment
    assert {g['id']: g['content']['total'] for g in ws.gradings(pid, assignment_id=aid)} == before
    assert ws.overview(pid)['assignment_id'] == aid  # the project view opens on it
    view = ws.assignment(aid)
    assert view['class_name'] == '高一（3）班' and view['status'] == 'done' and view['roster'] == 2
    assert ws.home()['legacy_projects'] == 0
    with pytest.raises(Conflict):
        ws.map_projects([pid], course_id=out['course_id'])


def test_topics_fill_in_with_work_and_show_what_was_taught_and_tested(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)  # knowledge_module 对数函数 -> topic 对数函数
    [unit] = ws.units(crs['id'])
    assert unit['title'] == '对数函数' and unit['tasks'] == 1
    other = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')
    assert other['project']['unit_id'] == unit['id'] and ws.units(crs['id'])[0]['tasks'] == 2

    a3 = ws.assign(pid, o3['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    out = ws.analyze(pid, assignment_id=a3['id'])
    ws.confirm(out['patterns'][0]['id'])
    view = ws.unit_view(unit['id'])
    assert [o['text'] for o in view['objectives']] == ['能在解对数不等式时检查定义域']
    [cls] = view['classes']  # class 5 has nothing in this topic yet
    assert cls['class_name'] == '高一（3）班' and len(cls['sessions']) == 1 and len(cls['assignments']) == 1
    assert cls['evidence'] == []  # the analysis is not confirmed yet: no attainment shown
    assert cls['patterns'][0]['name'] == '解对数不等式时遗漏定义域'
    ws.confirm(out['analysis']['id'])
    [ev] = ws.unit_view(unit['id'])['classes'][0]['evidence']
    assert ev['objectives'][0]['得分率'] == 0.5 and not ev['partial']

    renamed = ws.rename_unit(unit['id'], '对数函数与对数不等式')
    assert renamed['title'] == '对数函数与对数不等式'
    ws.set_project_unit(other['project']['id'], '')
    assert ws.units(crs['id'])[0]['tasks'] == 1


def test_explanation_without_analysis_stays_about_one_answer(ws):
    p = ws.start_task(ws.create_course('高一数学', '数学')['id'], 'explain', knowledge_module='对数函数')['project']
    reply = {
        'focus': ['先写定义域'],
        'steps': ['x-3>0', 'x-3<4', '取交集'],
        'pitfalls': ['漏写定义域'],
        'answer_comment': '这份作答漏了定义域。',
        'quotes': ['x<7', '学生没写过这句'],
        'follow_up': ['为什么要先写 x-3>0？'],
    }
    ws.llm.handlers['explain'] = lambda r, payload: reply
    general = ws.explain(p['id'], '解不等式 log2(x-3)<2', ask='讲清定义域')
    g = general['explanation']['content']
    assert g['mode'] == 'general' and g['answer_comment'] == '' and g['quotes'] == [] and '预判' in g['scope_note']
    assert '学生作答' not in ws.llm.calls[-1]['user'] and general['context_note'].startswith('已参考：项目背景')
    one = ws.explain(p['id'], '解不等式 log2(x-3)<2', student_answer='x-3<4，所以 x<7')['explanation']['content']
    assert one['mode'] == 'answer' and one['quotes'] == ['x<7'] and one['quotes_rejected'] == 1
    assert '不代表全班' in one['scope_note']
    assert len(ws.explanations(p['id'])) == 1  # same question: a new version of the same draft


def test_class_adapted_copy_keeps_its_source_and_its_own_versions(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    out = ws.derive_for_class(pid, o5['id'])
    new_pid = out['project']['id']
    assert out['copied'] == {'plan': True, 'questions': 1, 'materials': 1}
    assert out['project']['offering_id'] == o5['id'] and out['project']['unit_id'] == ws.units(crs['id'])[0]['id']
    [nq] = ws.questions(new_pid)
    assert nq['status'] == 'draft' and nq['origin'] == 'derived' and nq['edit_of'] == q['id']
    assert nq['lineage_id'] != q['lineage_id'] and nq['rubric']['content']['question_lineage'] == nq['lineage_id']
    assert nq['content']['derived_from'] == {'project': pid, 'artifact': q['id']}
    # Class 5 adapts its copy; class 3's version is untouched.
    edited = ws.edit(nq['id'], {'stem': '解不等式 log2(x-3)<2，并说明每一步的依据（提升）'})
    ws.confirm(edited['id'])
    assert ws.questions(pid)[0]['content']['stem'] == q['content']['stem']
    ws.confirm(ws.lesson_plans(new_pid)[0]['id'])
    assert [s['project_id'] for s in ws.sessions(o5['id'])] == [new_pid]


def test_teacher_removes_class_status_items_from_what_the_model_reads(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    a3 = ws.assign(pid, o3['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    out = ws.analyze(pid, assignment_id=a3['id'])
    ws.confirm(out['patterns'][0]['id'])
    ws.confirm(out['analysis']['id'])

    nxt = ws.start_task(crs['id'], 'prep', offering_id=o3['id'], knowledge_module='对数函数')['project']['id']
    status = ws.task_class_status(nxt)
    [row] = status['patterns']
    assert not row['excluded'] and status['summary']
    status = ws.set_status_prefs(nxt, exclude=[row['lineage']], exclude_summary=True)
    assert status['patterns'][0]['excluded'] and status['summary_excluded']  # still listed, so it can be put back
    assert '本班学情' not in ws.draft_lesson_plan(nxt)['context_note']  # nothing left to send
    ws.set_status_prefs(nxt, exclude=[])
    plan = ws.draft_lesson_plan(nxt)
    sent = ws.llm.calls[-1]['user']['本班学情']
    assert '本班学情' in plan['context_note'] and sent['问题模式'] and not sent['学情概述']


def test_missing_question_is_not_a_completed_assignment(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    original = ws.questions(pid)[0]
    second = ws.create_question(pid, {**original['content'], 'no': 2, 'stem': '第二题'}, original['rubric']['content'])
    ws.confirm(second['id'])
    assignment = ws.assign(pid, o3['id'])
    aid = assignment['id']
    graded(ws, pid, aid, f'甲,1,{GOOD}\n乙,1,{GOOD}')
    ws.confirm_gradings(pid, assignment_id=aid)
    view = ws.assignment(aid)
    assert view['status'] == 'partial' and view['expected'] == 4 and view['missing'] == 2
    with pytest.raises(Conflict, match='缺少 2 份'):
        ws.analyze(pid, assignment_id=aid)
    partial = ws.analyze(pid, assignment_id=aid, partial=True)['analysis']['content']
    assert partial['partial'] and partial['coverage']['missing'] == 2
    # An explicitly imported blank answer is different from an absent record.
    ws.import_submissions(pid, '甲,2,\n乙,2,', assignment_id=aid)
    view = ws.assignment(aid)
    assert view['missing'] == 0 and view['blank'] == 2 and view['status'] == 'grading'
    ws.grade_round(pid, assignment_id=aid)
    ws.confirm_gradings(pid, assignment_id=aid)
    assert ws.assignment(aid)['status'] == 'done'
    assert not ws.analyze(pid, assignment_id=aid)['analysis']['content']['partial']
