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
    sent = ws.llm.last('draft_lesson_plan')['user']['本班学情']
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
    assert '学生作答' not in ws.llm.calls[-1]['user'] and general['context_note'].startswith('已参考：课时背景')
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
    sent = ws.llm.last('draft_lesson_plan')['user']['本班学情']
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


# --- 跳步：从首页直接开始的任务，落到对应页面，并把"为什么进不去、怎么进"说清楚 ---


def test_grading_task_opens_the_grading_page(ws):
    crs = ws.create_course('高一数学', '数学')
    started = ws.start_task(crs['id'], 'grading')
    assert started['tab'] == 'grading' and started['task']['kind_label'] == '批改作业'


def test_new_grading_task_says_what_is_missing_and_how_to_get_there(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'grading')['project']['id']
    b = ws.overview(pid)['blockers']
    # 出题不必先备课；批改任务缺题目时，优先导入现成试卷。
    assert not b['questions']['blocked'] and b['grading']['blocked']
    assert b['grading']['action']['label'] == '导入现成试卷' and b['grading']['action']['target'] == '#g-paper-text'
    assert b['grading']['reason']
    assert b['prep']['blocked'] is False and b['review']['blocked'] is False


def test_importing_a_paper_unblocks_the_questions_page_without_a_plan(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'grading')['project']['id']
    from test_edu_workflow import question

    ws.llm.handlers['import_paper'] = lambda r, payload: {
        'objectives': [{'text': '能在解对数不等式时检查定义域', 'success_criterion': '写出 x>1'}],
        'questions': [question('解不等式 log2(x-1) < 3')],
    }
    ws.import_paper(pid, '1. 解不等式 log2(x-1) < 3')
    ov = ws.overview(pid)
    assert ov['counts']['question']['total'] == 1
    assert ov['blockers']['questions']['blocked'] is False
    # 题目还是草稿：批改仍被挡住，但下一步是"确认题目"
    assert ov['blockers']['grading']['blocked'] and ov['blockers']['grading']['action']['target'] == '#questions'
    [q] = ws.questions(pid)
    ws.confirm(q['id'])
    assert ws.overview(pid)['blockers']['grading']['blocked'] is False


def test_blocker_for_analysis_points_at_confirming_gradings(ws):
    crs, o3, _ = two_classes(ws)
    pid, _q = prep_and_practice(ws, crs, o3)
    aid = ws.assign(pid, o3['id'])['id']
    ws.import_submissions(pid, f'甲,1,{GOOD}', assignment_id=aid)
    ws.grade_round(pid, assignment_id=aid)
    b = ws.overview(pid)['blockers']
    assert b['analysis']['blocked'] and b['analysis']['action']['target'] == '#confirm-auto'


def test_task_without_a_topic_is_named_so_it_can_be_told_apart(ws):
    crs = ws.create_course('高一数学', '数学')
    t = ws.start_task(crs['id'], 'grading')
    assert t['task']['title'] == '高一数学·批改作业'
    named = ws.start_task(crs['id'], 'prep', knowledge_module='对数函数', lesson='第2课时')
    assert named['task']['title'] == '对数函数｜第2课时'


def test_marking_a_task_done_returns_the_task(ws):
    """标记完成后回查任务时不能只查"进行中"，否则刚改完的任务自己找不到。"""
    crs = ws.create_course('高一数学', '数学')
    t = ws.start_task(crs['id'], 'prep', knowledge_module='对数函数')
    done = ws.update_task(t['task']['id'], status='done')
    assert done['status'] == 'done' and done['title'] == '对数函数'
    assert ws.tasks(crs['id']) == []  # 不再出现在"继续工作"里
    assert ws.tasks(crs['id'], status=None)[0]['status'] == 'done'
    # 再改一次（比如改回进行中）同样要能查到
    reopened = ws.update_task(t['task']['id'], status='open')
    assert reopened['status'] == 'open' and ws.tasks(crs['id'])[0]['id'] == t['task']['id']


def test_task_navigation_matches_intent_and_existing_work(ws):
    crs = ws.create_course('高一数学', '数学')
    task = ws.start_task(crs['id'], 'grading', knowledge_module='对数函数')
    pid = task['project']['id']
    overview = ws.overview(pid)
    assert overview['next'][0]['target'] == '#g-paper-text'
    assert overview['blockers']['grading']['action']['target'] == '#g-paper-text'
    card = ws.tasks(crs['id'])[0]
    assert card['action_label'] == '导入试卷' and card['tab'] == 'grading'
    from test_edu_workflow import question

    ws.llm.handlers['import_paper'] = lambda r, payload: {
        'objectives': [{'text': '能解对数不等式', 'success_criterion': '写出定义域'}],
        'questions': [question('解不等式 log2(x-1) < 3')],
    }
    ws.import_paper(pid, '1. 解不等式 log2(x-1) < 3')
    assert ws.overview(pid)['next'][0]['target'] == '#questions'
    ws.confirm(ws.questions(pid)[0]['id'])
    assert ws.overview(pid)['next'][0]['tab'] == 'grading'


def test_task_cards_distinguish_empty_draft_and_confirmed_plan(ws):
    crs = ws.create_course('高一数学', '数学')
    task = ws.start_task(crs['id'], 'prep', knowledge_module='对数函数', title='周三公开课')
    pid = task['project']['id']
    card = ws.tasks(crs['id'])[0]
    assert card['title'] == '周三公开课' and card['progress_label']
    plan = ws.draft_lesson_plan(pid)['lesson_plan']
    card = ws.tasks(crs['id'])[0]
    assert card['action_label'] == '审阅教案' and card['target'] == '#lesson-plan'
    ws.confirm(plan['id'])
    assert ws.tasks(crs['id'])[0]['action_label'] == '查看教案'


def test_direct_explanation_does_not_require_a_lesson_plan(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'explain')['project']['id']
    action = ws.overview(pid)['next'][0]
    assert action['tab'] == 'review' and action['target'] == '#ex-question'


def test_task_card_follows_the_loop_and_stale_items_say_what_they_are(ws):
    crs, o3, _ = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    assert ws.tasks(crs['id'])[0]['action_label'] != '查看教案'  # a 备课 task that has questions moved on
    a3 = ws.assign(pid, o3['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    card = ws.tasks(crs['id'])[0]
    assert (card['tab'], card['target']) == ('grading', '#confirm-auto')
    assert card['assignment_id'] == a3['id'] and card['scope_label'] == '高一（3）班 · 第 1 轮'
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    ws.analyze(pid, assignment_id=a3['id'])
    assert ws.tasks(crs['id'])[0]['tab'] == 'analysis'

    rubric = ws.correction_impact(a3['id'], q['lineage_id'])['rubric']['content']
    ws.correct_rubric(a3['id'], q['lineage_id'], {'points': [{**rubric['points'][0], 'score': 3}, rubric['points'][1]]})
    card = ws.tasks(crs['id'])[0]
    assert card['tab'] == 'records' and card['action_label'] == '更新受影响的结果'
    [old] = [m for m in ws.stale(pid) if m['type'] == 'analysis']
    assert old['class_name'] == '高一（3）班' and old['detail'] == '完整分析' and not old['redone']
    assert {m['detail'] for m in ws.stale(pid) if m['type'] == 'grading'} == {'甲 第 1 题', '乙 第 1 题'}

    ws.grade_round(pid, assignment_id=a3['id'])
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    ws.analyze(pid, assignment_id=a3['id'])
    [old] = [m for m in ws.stale(pid) if m['type'] == 'analysis']
    assert old['redone']  # a newer analysis of the same class and round exists: one click resolves it


def test_practice_task_writes_questions_without_a_lesson_plan(ws):
    crs = ws.create_course('高一语文', '语文')
    pid = ws.start_task(crs['id'], 'practice')['project']['id']
    step = ws.overview(pid)['next'][0]
    assert (step['tab'], step['target']) == ('questions', '#q-topic')  # not back to 01
    with pytest.raises(Conflict):
        ws.generate_questions(pid)  # nothing to work from yet
    from test_edu_workflow import question

    ws.llm.handlers['generate_questions'] = lambda r, p: {
        'questions': [{**question('词中“粪土当年万户侯”表达了怎样的情怀？'), 'type': 'short_answer'}]
    }
    ws.update_project(pid, knowledge_module='《沁园春·长沙》')
    assert ws.overview(pid)['next'][0]['target'] == '#generate-questions'
    out = ws.generate_questions(pid, types=['short_answer'], count=1, ab=False)
    [q] = out['candidates'][0]['questions']
    plan = ws.lesson_plans(pid)[0]
    assert plan['status'] == 'draft' and all(o['inferred'] for o in plan['content']['objectives'])
    assert '《沁园春·长沙》' in plan['content']['objectives'][0]['text']
    cov = ws.coverage(pid)
    assert cov['unconfirmed'] and cov['draft_kind'] == 'inferred' and cov['objectives'][0]['total'] == 1
    assert q['content']['objective_ids'] == [cov['objectives'][0]['id']]
    ws.generate_questions(pid, types=['short_answer'], count=1, ab=False)  # reuses the inferred objectives
    assert sum(1 for x in ws.llm.calls if x['task'] == 'infer_objectives') == 1


def test_a_plan_drafted_in_prep_is_confirmed_before_questions(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    ws.draft_lesson_plan(pid)
    assert ws.overview(pid)['blockers']['questions']['action']['target'] == '#lesson-plan'
    # The questions page says the objectives come from 01's draft, not that they were inferred from a request.
    assert ws.coverage(pid)['draft_kind'] == 'teacher_draft'
    with pytest.raises(Conflict) as err:
        ws.generate_questions(pid)
    assert '01' in str(err.value)


def test_progress_reports_the_current_step_while_generating(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    seen = {}
    gen, solve = ws.llm.handlers['generate_questions'], ws.llm.handlers['self_check_solve']

    def watch(name, inner):
        def handler(r, p):
            seen.setdefault(name, ws.progress(pid))
            return inner(r, p)

        return handler

    ws.llm.handlers['generate_questions'] = watch('generate', gen)
    ws.llm.handlers['self_check_solve'] = watch('solve', solve)
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    [g] = seen['generate']
    assert (g['label'], g['step']) == ('出题', '出题') and g['elapsed'] >= 0
    [s] = seen['solve']
    assert s['step'] == '独立解题与审核' and (s['done'], s['total']) == (0, 1)
    assert ws.progress(pid) == []  # nothing left once the run ended
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    assert ws.progress(pid) == [] and seen['generate'][0]['typical'] is None  # first run had no history yet


def test_analysis_blocked_by_missing_questions_points_to_the_questions_page(ws):
    crs = ws.create_course('高一语文', '语文')
    pid = ws.start_task(crs['id'], 'practice')['project']['id']
    action = ws.overview(pid)['blockers']['analysis']['action']
    assert (action['label'], action['tab']) == ('先到 02 准备题目', 'questions')


def test_inferred_objectives_edited_on_the_questions_page_still_allow_questions(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    cov = ws.coverage(pid)
    plan = ws.artifact(cov['plan_id'])
    objectives = [{**o, 'text': '能解简单的对数不等式'} for o in plan['content']['objectives']]
    ws.edit(cov['plan_id'], {**plan['content'], 'objectives': objectives})
    cov = ws.coverage(pid)
    assert cov['unconfirmed'] and cov['objectives'][0]['text'] == '能解简单的对数不等式'
    assert not ws.overview(pid)['blockers']['questions']['blocked']
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)


def _meet(handler, barrier):
    """A fake model call that only returns once its partner call has started too (fails if they run in turn)."""

    def call(r, p):
        barrier.wait()
        return handler(r, p)

    return call


def test_review_and_independent_solution_run_side_by_side(ws):
    import threading

    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    both = threading.Barrier(2, timeout=5)
    for task in ('review_question', 'self_check_solve'):
        ws.llm.handlers[task] = _meet(ws.llm.handlers[task], both)
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    checks = q['content']['checks']
    assert checks['solver'] == 'agree' and checks['difficulty'] == '中等'  # both results merged into the question


def test_two_ab_versions_are_generated_side_by_side(ws):
    import threading

    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    ws.generate_questions(pid, types=['calculation'], count=1, ab=False)  # objectives first, outside the race
    import itertools

    from test_edu_workflow import question

    n = itertools.count(1)  # two different versions, or the comparison is dropped as a duplicate
    gen = lambda r, p: {'questions': [question(f'解不等式 log2(x-{next(n)}) < 3')]}  # noqa: E731
    ws.llm.handlers['generate_questions'] = _meet(gen, threading.Barrier(2, timeout=5))
    out = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)
    assert [c['position'] for c in out['candidates']] == ['A', 'B'] and out['candidate_group']


def _two_goal_task(ws):
    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    ws.llm.handlers['infer_objectives'] = lambda r, p: {
        'objectives': [
            {'text': '能解简单对数不等式', 'success_criterion': '写出定义域'},
            {'text': '能比较对数值大小', 'success_criterion': '说明单调性'},
        ]
    }
    return pid


def test_questions_are_written_one_objective_per_call(ws):
    from test_edu_workflow import question

    pid = _two_goal_task(ws)
    seen = []

    def gen(r, p):
        seen.append(([g['目标'] for g in p['教学目标']], None))
        return {'questions': [question(f'第 {len(seen)} 组：{p["教学目标"][0]["目标"]}')]}

    ws.llm.handlers['generate_questions'] = gen
    out = ws.generate_questions(pid, types=['calculation', 'fill_blank'], count=1, ab=False)
    assert sorted(g for g, _ in seen) == [['能比较对数值大小'], ['能解简单对数不等式']]  # one call each
    qs = out['candidates'][0]['questions']
    cov = {o['text']: o['id'] for o in ws.coverage(pid)['objectives']}
    by_goal = {q['content']['objective_ids'][0]: q['content']['stem'] for q in qs}
    assert by_goal[cov['能解简单对数不等式']].endswith('能解简单对数不等式')  # mapped back to the plan's own ids
    assert by_goal[cov['能比较对数值大小']].endswith('能比较对数值大小')
    assert sorted(q['content']['no'] for q in qs) == [1, 2] and len(out['candidates'][0]['generation_ids']) == 2


def test_a_failed_part_costs_only_its_own_questions(ws):
    from test_edu_workflow import question

    pid = _two_goal_task(ws)

    def gen(r, p):
        if p['教学目标'][0]['目标'] == '能比较对数值大小':
            raise RuntimeError('输出达到长度上限被截断')
        return {'questions': [question('解不等式 log2(x-1) < 3')]}

    ws.llm.handlers['generate_questions'] = gen
    out = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)
    [cand] = out['candidates']
    assert len(cand['questions']) == 1 and any('第 2 条目标' in d for d in cand['dropped'])


def test_choosing_an_ab_version_promotes_all_its_parts(ws):
    import itertools

    from test_edu_workflow import question

    pid = _two_goal_task(ws)
    n = itertools.count(1)
    ws.llm.handlers['generate_questions'] = lambda r, p: {'questions': [question(f'解不等式 log2(x-{next(n)}) < 3')]}
    out = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)
    assert [len(c['questions']) for c in out['candidates']] == [2, 2]
    res = ws.choose(pid, out['candidate_group'], 'B')
    assert len(res['promoted']) == 4  # two questions and their two rubrics
    drafts = [q for q in ws.questions(pid) if q['status'] == 'draft']
    assert sorted(q['content']['no'] for q in drafts) == [1, 2]


def test_standards_confirmation_respects_the_teacher_and_follows_the_course_info(ws):
    from edu_core.llm import ModelUnavailable

    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'prep', knowledge_module='对数', lesson='第2课时')['project']['id']
    ws.update_project(pid, lesson_scope='对数的运算性质及其简单应用')

    def down(r, p):
        raise ModelUnavailable('网关超时')

    ws.llm.handlers['pick_standards'] = down
    ws.draft_lesson_plan(pid)
    assert ws.curriculum(pid)['chosen_by'] == 'auto'  # a failed confirmation keeps the automatic pick

    ws.update_project(pid, lesson_scope='对数的运算性质')  # the course info moved: the pick follows it
    ws.llm.handlers['pick_standards'] = lambda r, p: {'ids': [], 'note': '课标未单列这一内容'}
    ws.draft_lesson_plan(pid)
    cur = ws.curriculum(pid)
    assert cur['chosen_by'] == 'model' and cur['selected'] == [] and cur['note'] == '课标未单列这一内容'
    assert '课程标准' not in ws.llm.last('draft_lesson_plan')['user']  # no standard reaches the plan

    ws.update_project(pid, curriculum_ids=['math.B2.2.3.1'])
    calls = len(ws.llm.calls)
    ws.draft_lesson_plan(pid)
    assert not [x for x in ws.llm.calls[calls:] if x['task'] == 'pick_standards']  # the teacher's pick is final


def test_parts_see_the_other_objectives_and_near_duplicates_get_a_suggestion(ws):
    from test_edu_workflow import question

    pid = _two_goal_task(ws)
    seen = []

    def gen(r, p):
        seen.append(p['其他目标'])
        return {'questions': [question('已知 lg2=a，lg3=b，用 a、b 表示 lg12 的值')]}  # both parts write the same

    ws.llm.handlers['generate_questions'] = gen
    qs = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    assert sorted(seen) == [['能比较对数值大小'], ['能解简单对数不等式']]
    checks = [q['content']['checks'] for q in qs]
    assert not any('内容相近' in n for n in checks[0]['notes']) and '第 1 道题内容相近' in checks[1]['notes'][-1]
    assert not checks[1]['flags']  # a near duplicate is a note in AI 提示, never 需核对


def test_the_solver_reports_given_numbers_the_data_cannot_produce(ws):
    crs = ws.create_course('高二人工智能', '人工智能')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='决策树')['project']['id']
    ws.llm.handlers['self_check_solve'] = lambda r, p: {
        'answer': '…', 'final_answer': '1<x<9', 'data_issue': '8 个样本的二分类熵不可能是 0.94，应为 0.954 或 1',
    }
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    assert q['content']['checks']['flags'][-1] == '数据问题：8 个样本的二分类熵不可能是 0.94，应为 0.954 或 1'


def test_plan_has_examples_and_exit_checks_and_is_reviewed_on_request(ws):
    from edu_core.llm import ModelUnavailable
    from test_edu_workflow import HANDLERS

    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'prep', knowledge_module='对数', lesson='第2课时')['project']['id']
    base = HANDLERS['draft_lesson_plan']

    def plan(r, p):
        out = base(r, p)
        out['objectives'] = [out['objectives'][0], {**out['objectives'][0], 'text': '能比较两个对数值的大小'}]
        out['activities'] = [{'name': '例题', 'minutes': 15, 'description': '板演', 'objective_index': 0, 'evidence': ''}]
        out['examples'] = [{'objective_index': 0, 'problem': '解 log2(x-1)<3', 'solution': '定义域 x>1；单调递增得 x<9；1<x<9'}]
        out['exit_check'] = [{'objective_index': 1, 'question': '比较 log2 3 与 log2 5', 'answer': 'log2 3 < log2 5'}]
        return out

    ws.llm.handlers['draft_lesson_plan'] = plan
    ws.llm.handlers['check_plan'] = lambda r, p: {'issues': ['例题 1 的解答缺少单调性的依据，补上“底数大于 1 递增”']}
    plan = ws.draft_lesson_plan(pid)['lesson_plan']
    content = plan['content']
    assert 'check_plan' not in ws.llm.tasks() and 'review' not in content  # the teacher reads the plan first
    assert content['examples'][0]['objective_ids'] == ['O1'] and content['exit_check'][0]['objective_ids'] == ['O2']
    assert not {'key_questions', 'homework', 'board'} & set(content)  # only what is needed to teach from
    o1, o2 = content['objectives']
    assert '课上没有安排检验这条目标的环节' in o1.get('checks', [])  # its only step checks nothing
    assert '课上没有安排检验这条目标的环节' not in o2.get('checks', [])  # the exit check checks it

    # AI 帮我看看: only when the teacher asks, written as the teacher sees the plan, not stored in it
    assert ws.check_plan(plan['id']) == {'issues': ['例题 1 的解答缺少单调性的依据，补上“底数大于 1 递增”']}
    seen = ws.llm.last('check_plan')['user']['备课方案']
    assert seen['例题'][0] == {'编号': '例题 1', '对应': '目标 1', '题目': '解 log2(x-1)<3', '解答': '定义域 x>1；单调递增得 x<9；1<x<9'}
    assert 'objective_ids' not in str(seen) and 'O1' not in str(seen)
    assert 'review' not in ws.artifact(plan['id'])['content']

    def down(r, p):
        raise ModelUnavailable('网关超时')

    ws.llm.handlers['check_plan'] = down
    failed = ws.check_plan(plan['id'])
    assert failed['issues'] == [] and failed['error'].startswith('这次没有看成')  # never passed off as clean


def test_an_example_can_be_revised_on_its_own(ws):
    from test_edu_workflow import HANDLERS

    crs = ws.create_course('高二人工智能', '人工智能')
    pid = ws.start_task(crs['id'], 'prep', knowledge_module='决策树', lesson='第4课时')['project']['id']
    base = HANDLERS['draft_lesson_plan']
    ws.llm.handlers['draft_lesson_plan'] = lambda r, p: {**base(r, p), 'examples': [
        {'objective_index': 0, 'problem': '6 个样本 3 是 3 否，求根节点熵', 'solution': 'H=1'}]}
    plan = ws.draft_lesson_plan(pid)['lesson_plan']
    ws.llm.handlers['revise_plan'] = lambda r, p: {
        'text': '', 'success_criterion': '', 'name': '', 'minutes': 0, 'description': '', 'items': [],
        'question': '6 个样本 4 是 2 否，求根节点熵', 'answer': 'H≈0.918'}
    out = ws.revise_plan_part(plan['id'], 'example:1', '数据改成 4 是 2 否并重算')
    assert out['lesson_plan']['content']['examples'][0] == {'problem': '6 个样本 4 是 2 否，求根节点熵', 'solution': 'H≈0.918', 'objective_ids': ['O1']}
    sent = ws.llm.last('revise_plan')['user']
    assert sent['要修改的部分']['位置'] == '例题 1' and '例题' in sent['整份方案'] and 'objectives' not in sent['整份方案']
    assert out['lesson_plan']['content']['revision']['label'] == '例题 1'  # shown to the teacher, not example:1


def test_a_question_is_changed_by_the_teachers_remark_not_replaced(ws):
    import pytest as _pytest

    from test_edu_workflow import question

    crs = ws.create_course('高一数学', '数学')
    pid = ws.start_task(crs['id'], 'practice', knowledge_module='对数函数')['project']['id']
    [q] = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions']
    ws.llm.handlers['revise_question'] = lambda r, p: {'questions': [question('解不等式 log2(x-1) < 2')]}
    with _pytest.raises(ValueError):
        ws.revise_question(q['id'], '  ')
    new = ws.revise_question(q['id'], '把右边的 3 改成 2')['question']
    sent = ws.llm.last('revise_question')
    assert sent['user']['老师的修改意见'] == '把右边的 3 改成 2' and sent['user']['原题']['题干'] == q['content']['stem']
    assert sent['user']['原题']['评分要点'] and '保持原样' in sent['system']  # the whole question, changed only as asked
    assert (new['lineage_id'], new['version'], new['content']['no']) == (q['lineage_id'], 2, q['content']['no'])
    assert new['content']['objective_ids'] == q['content']['objective_ids'] and new['content']['revised_from'] == q['id']
    assert new['content']['stem'] == '解不等式 log2(x-1) < 2' and 'checks' in new['content']  # checked like a new one


def test_keeping_the_original_version_stops_the_reminder_until_a_newer_one(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    a5 = ws.assign(pid, o5['id'])

    def new_rubric_version():
        rubric = ws.questions(pid)[0]['rubric']
        pts = rubric['content']['points']
        extra = {'id': f'P{len(pts) + 1}', 'description': '书写规范', 'score': 1}
        new = ws.edit(rubric['id'], {'points': [*pts, extra]})
        ws.confirm(new['id'], change_kind='substantive')

    new_rubric_version()
    assert ws.assignment(a5['id'])['updates'] and any(x['kind'] == 'update' for x in ws.home(crs['id'])['attention'])
    ws.keep_pinned(a5['id'], q['lineage_id'])
    assert ws.assignment(a5['id'])['updates'] == []
    assert not any(x['kind'] == 'update' for x in ws.home(crs['id'])['attention'])
    new_rubric_version()  # a later version is a new decision: reported again
    assert ws.assignment(a5['id'])['updates'] == [{'question_lineage': q['lineage_id'], 'no': 1, 'changed': ['rubric']}]


def test_stale_results_of_another_class_do_not_take_over_the_next_step(ws):
    crs, o3, o5 = two_classes(ws)
    pid, q = prep_and_practice(ws, crs, o3)
    a3, a5 = ws.assign(pid, o3['id']), ws.assign(pid, o5['id'])
    graded(ws, pid, a3['id'], f'甲,1,{GOOD}\n乙,1,{MISSED}')
    graded(ws, pid, a5['id'], f'丙,1,{GOOD}\n丁,1,{MISSED}')
    ws.confirm_gradings(pid, assignment_id=a3['id'])
    rubric = ws.correction_impact(a3['id'], q['lineage_id'])['rubric']['content']
    ws.correct_rubric(a3['id'], q['lineage_id'], {'points': [{**rubric['points'][0], 'score': 3}, rubric['points'][1]]})

    here = ws.overview(pid, assignment_id=a3['id'])['next']
    assert here[0]['label'] == '更新受影响的结果' and not here[0].get('optional')
    there = ws.overview(pid, assignment_id=a5['id'])['next']
    assert there[0]['tab'] == 'grading' and not there[0].get('optional')  # 5班's own loop comes first
    assert there[-1]['label'] == '更新其他班级或轮次的结果' and there[-1]['optional']
