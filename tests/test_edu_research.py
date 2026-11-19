"""Course objectives, unit review, phase C (A/B records, partial revision, materials, deliverables, coursemaker) and
run records."""

import io
import json
import random
import zipfile

import pytest
from edu_fake import FakeLLM
from test_edu_course import GOOD, MISSED, graded, prep_and_practice, two_classes
from test_edu_workflow import HANDLERS, question

from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    handlers = dict(HANDLERS)
    handlers['regenerate_question'] = HANDLERS['generate_questions']
    handlers['revise_plan'] = lambda r, p: {
        'text': '',
        'success_criterion': '',
        'name': '复习定义域',
        'minutes': 3,
        'description': '口答两题',
        'items': [],
    }
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


# ----- A/B records -----


def ab_project(ws, stems):
    p = ws.create_project('物理', 'physics', knowledge_module='匀速直线运动')
    ws.confirm(ws.draft_lesson_plan(p['id'])['lesson_plan']['id'])
    it = iter(stems)
    ws.llm.handlers['generate_questions'] = lambda r, payload: {'questions': [question(next(it))]}
    return p['id']


def test_ab_records_input_display_and_separate_feedback(ws):
    pid = ab_project(ws, ['甲题', '乙题', '丙题', '丁题'])
    gen = ws.generate_questions(pid, types=['calculation'], ab=True)
    group = gen['candidate_group']
    with ws.store.read() as c:
        cmp = dict(c.execute('SELECT * FROM comparisons WHERE candidate_group=?', (group,)).fetchone())
    assert cmp['trigger'] == 'requested' and cmp['status'] == 'shown' and set(json.loads(cmp['display'])) == {'A', 'B'}
    with pytest.raises(ValueError):
        ws.choose(pid, group, 'both_ok', preference='better')  # “better” only for an adopted side
    out = ws.choose(pid, group, 'both_ok', work_choice='B', reason_tags=['更清楚', '乱写'])
    assert len(out['promoted']) == 2
    with ws.store.read() as c:
        ch = dict(c.execute('SELECT * FROM choices WHERE id=?', (out['choice_id'],)).fetchone())
    assert ch['acceptable'] == 'both' and ch['work_choice'] == 'B' and ch['preference'] == 'none'
    assert json.loads(ch['reason_tags']) == ['更清楚']
    ws.revoke_choice(out['choice_id'])
    assert ws.overview(pid)['pending_ab'] == [group]  # open again after the withdrawal
    ws.choose(pid, group, 'A', preference='better')

    second = ws.generate_questions(pid, types=['calculation'], ab=True)
    [a_q] = [q for c_ in second['candidates'] if c_['position'] == 'A' for q in c_['questions']]
    [b_q] = [q for c_ in second['candidates'] if c_['position'] == 'B' for q in c_['questions']]
    merged = ws.merge_candidates(
        pid,
        second['candidate_group'],
        {'type': 'calculation', 'stem': '结合两份：' + a_q['content']['stem']},
        {'points': [{'description': '结果', 'score': 2}]},
        [a_q['id'], b_q['id']],
    )
    assert merged['origin'] == 'merged' and {r['role'] for r in merged['refs']} == {'merged_from'}


def test_duplicate_or_failed_candidates_are_not_compared(ws):
    pid = ab_project(ws, ['同一题', '同一题'])
    gen = ws.generate_questions(pid, types=['calculation'], ab=True)
    assert gen['candidate_group'] is None and '几乎相同' in gen['ab_note']
    assert ws.questions(pid)[0]['status'] == 'draft' and ws.overview(pid)['pending_ab'] == []

    calls = iter([None, RuntimeError('超时')])

    def flaky(r, payload):
        err = next(calls)
        if err:
            raise err
        return {'questions': [question('只有一份')]}

    ws.llm.handlers['generate_questions'] = flaky
    gen = ws.generate_questions(pid, types=['calculation'], ab=True)
    assert gen['candidate_group'] is None and '生成失败' in gen['ab_note']


def test_sampled_comparisons_respect_the_daily_cap(tmp_path):
    settings = load_settings(db=str(tmp_path / 'e.sqlite'), audit_rate=0.0, ab_rate=1.0, ab_daily_cap=1, concurrency=1)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(3))
    pid = ab_project(ws, [f'题{i}' for i in range(8)])
    first = ws.generate_questions(pid, types=['calculation'])
    second = ws.generate_questions(pid, types=['calculation'])
    assert first['candidate_group'] and second['candidate_group'] is None


# ----- partial revision, materials, deliverables, coursemaker -----


def test_partial_revision_changes_only_the_target(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    plan = ws.lesson_plans(pid)[0]
    out = ws.revise_plan_part(plan['id'], 'activity:1', '缩短导入活动')
    new = out['lesson_plan']['content']
    assert out['changed'] == ['activities'] and new['activities'][0]['minutes'] == 3
    assert (
        new['activities'][1:] == plan['content']['activities'][1:]
        and new['objectives'] == plan['content']['objectives']
    )
    assert new['revision'] == {'target': 'activity:1', 'instruction': '缩短导入活动', 'changed': ['activities']}
    assert out['lesson_plan']['status'] == 'draft' and out['lesson_plan']['edit_of'] == plan['id']
    with pytest.raises(ValueError):
        ws.revise_plan_part(plan['id'], 'activity:9', '改')


def test_materials_can_be_excluded_or_narrowed_to_a_section(ws):
    p = ws.create_project('对数', '数学', knowledge_module='对数函数')
    keep = ws.add_material(
        p['id'], '# 第一章 指数\n指数内容\n# 第二章 对数\n对数定义与性质\n# 第三章 幂函数\n幂函数内容', filename='教材'
    )
    drop = ws.add_material(p['id'], '无关的旧讲义', filename='旧讲义')
    ws.set_material_prefs(p['id'], exclude=[drop['id']], focus={keep['id']: '第二章'})
    note = ws.draft_lesson_plan(p['id'])['context_note']
    assert '材料 2 份（1 份已排除，1 份只发送指定章节）' in note
    sent = ws.llm.calls[-1]['user']['参考材料']
    assert sent == [{'文件': '教材', '内容': '# 第二章 对数\n对数定义与性质\n'}]


def test_deliverables_hide_answers_from_students(ws):
    crs, o3, _ = two_classes(ws)
    t = ws.start_task(crs['id'], 'prep', offering_id=o3['id'], knowledge_module='对数函数')
    pid = t['project']['id']
    with pytest.raises(Conflict):
        ws.deliverable(pid, 'lesson_plan')  # nothing adopted yet: a clear refusal, not research JSON
    ws.confirm(ws.draft_lesson_plan(pid)['lesson_plan']['id'])
    q = ws.create_question(
        pid,
        {
            'type': 'single_choice',
            'stem': '下列函数是增函数的是',
            'options': ['y=log0.5 x', 'y=log2 x'],
            'answer_key': 'B',
            'reference': '底数大于 1',
        },
        {'points': [{'description': '选对', 'score': 3}]},
    )
    ws.confirm(q['id'])
    _, _, student = ws.deliverable(pid, 'student_paper')
    assert '下列函数是增函数的是' in student and 'B. y=log2 x' in student
    assert '答案' not in student and '底数大于 1' not in student and '选对' not in student
    _, _, key = ws.deliverable(pid, 'teacher_key')
    assert '答案：B' in key and '底数大于 1' in key and '题目第 1 版 · 评分标准第 1 版' in key
    name, media, data = ws.deliverable(pid, 'lesson_plan', 'docx')
    assert name.endswith('.docx') and media.startswith('application/vnd.openxmlformats')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        doc = z.read('word/document.xml').decode()
    assert '能在解对数不等式时检查定义域' in doc and '备课方案第 1 版（已确认）' in doc


def test_coursemaker_package_and_returned_courseware(ws):
    _crs, _o3, _o5, pid, _q, _a3 = analysed_class(ws)
    pkg = ws.courseware_package(pid)
    assert pkg['contract'].startswith('edu-harness/coursemaker-package') and pkg['student_text'] == 'none'
    assert pkg['coursemaker_chapter']['objectives'] == ['能在解对数不等式时检查定义域']
    assert pkg['objectives'][0]['code'] == 'G1' and {s['type'] for s in pkg['sources']} == {
        'lesson_plan',
        'question',
        'rubric',
    }
    blob = json.dumps(pkg, ensure_ascii=False)
    assert '甲' not in blob and MISSED not in blob  # no roster, no student answers
    cw = ws.import_courseware(
        pid, pkg['package_id'], 'slides', [{'name': '第2课时.pptx', 'url': 'https://example/x.pptx'}], 'job-1'
    )
    assert cw['type'] == 'courseware' and not cw['stale']
    plan = ws.lesson_plans(pid)[0]
    ws.confirm(ws.edit(plan['id'], {'notes': '改了活动'})['id'], change_kind='substantive')
    assert any(m['artifact_id'] == cw['id'] for m in ws.stale(pid))  # upstream changed: the courseware is flagged
    old = ws.import_courseware(pid, pkg['package_id'], 'handout', [])
    assert old['stale']  # made from a package whose plan is no longer current
