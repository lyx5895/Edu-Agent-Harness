"""Course objectives, unit review, phase C (A/B records, partial revision, materials, deliverables, coursemaker) and
phase D (runs and research counts, source groups, research cases, consent and training export)."""

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
    built = ws.build_research_cases(crs['id'])
    counts = built['counts']
    assert counts['teacher_tasks'] == 1 and counts['runs'] == 3 and counts['model_calls'] > counts['runs']
    assert counts['research_cases'] == 2 and counts['contributors'] is None  # plan + questions; no accounts
    assert ws.build_research_cases(crs['id'])['created'] == 0  # idempotent


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


def ab_project(ws, stems, alternatives=None):
    p = ws.create_project('物理', 'physics', knowledge_module='匀速直线运动')
    ws.confirm(ws.draft_lesson_plan(p['id'])['lesson_plan']['id'])
    a_stems = iter(stems)
    b_stems = iter(alternatives or [f'替代 {i}' for i in range(30)])

    def generate(role, payload, system):
        # The b arm is recognized by its variant line; both arms share the rest of the input.
        if '优先设计贴近真实情境' in system:
            return {'questions': [question(next(b_stems))]}
        n = int(payload.get('每个目标出题数', 1))
        return {
            'questions': [
                {**question(next(a_stems)), 'objective_index': i}
                for i in range(len(payload['教学目标']))
                for _ in range(n)
            ]
        }

    ws.llm.handlers['generate_questions'] = generate
    return p['id']


class ScriptedRng:
    """Feeds the per-objective sampling draws one by one, so a test says which objectives get a comparison."""

    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)

    def randrange(self, n):
        return 0

    def choice(self, seq):
        return seq[0]


def test_ab_compares_one_version_per_objective(ws):
    pid = ab_project(ws, ['原题一', '原题二'], ['替代一'])
    gen = ws.generate_questions(pid, types=['calculation'], count=2, ab=True)
    assert gen['candidates'][0]['position'] == 'A'
    [alt] = gen['alternatives']
    assert alt['paired_a_index'] in (0, 1)
    with ws.store.read() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM comparisons WHERE project_id=?', (pid,))]
    assert len(rows) == 1
    assert {r['unit'] for r in rows} == {'question'}
    assert {r['factor'] for r in rows} == {'prompt_variant_independent'}
    assert rows[0]['shown_first'] in ('A', 'B')
    display = json.loads(rows[0]['display'])
    assert (display['A']['question'], display['B']['question']) == (alt['question'], alt['alternative'])
    # the two A questions stay drafts; the independent B sits apart as a candidate
    assert sorted(q['status'] for q in ws.questions(pid)) == ['candidate', 'draft', 'draft']
    assert len(ws.overview(pid)['pending_ab']) == 1


def test_choosing_an_alternative_replaces_only_that_question(ws):
    pid = ab_project(ws, ['原题一', '原题二'], ['替代一'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=2, ab=True)['alternatives']
    out = ws.choose(pid, alt['group'], 'B', reason='更贴近生活')
    assert len(out['promoted']) == 2  # the question's next version and its rubric
    qs = {q['content']['no']: q for q in ws.questions(pid)}
    assert len(qs) == 2 and '替代一' in {q['content']['stem'] for q in qs.values()}
    assert alt['alternative'] not in {q['id'] for q in qs.values()}  # one question per slot, not two
    assert ws.overview(pid)['pending_ab'] == []


def test_keeping_the_original_drops_the_alternative(ws):
    pid = ab_project(ws, ['原题一'], ['替代一'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)['alternatives']
    with pytest.raises(ValueError):
        ws.choose(pid, alt['group'], 'both_ok', preference='better')  # “better” only for an adopted side
    out = ws.choose(pid, alt['group'], 'A', reason_tags=['更清楚', '乱写'])
    assert out['promoted'] == []
    with ws.store.read() as c:
        ch = dict(c.execute('SELECT * FROM choices WHERE id=?', (out['choice_id'],)).fetchone())
    assert ch['acceptable'] == 'A' and json.loads(ch['reason_tags']) == ['更清楚']
    assert [q['content']['stem'] for q in ws.questions(pid)] == ['原题一']
    assert ws.overview(pid)['pending_ab'] == []


def test_confirming_without_comparing_records_skip(ws):
    pid = ab_project(ws, ['原题一'], ['替代一'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)['alternatives']
    ws.confirm(alt['question'])
    assert ws.overview(pid)['pending_ab'] == []
    with ws.store.read() as c:
        ch = dict(
            c.execute(
                'SELECT * FROM choices WHERE candidate_group=? AND revoked_at IS NULL', (alt['group'],)
            ).fetchone()
        )
    assert ch['verdict'] == 'skip'


def test_revoking_restores_the_comparison(ws):
    pid = ab_project(ws, ['原题一'], ['替代一'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)['alternatives']
    out = ws.choose(pid, alt['group'], 'A')
    ws.revoke_choice(out['choice_id'])
    assert ws.overview(pid)['pending_ab'] == [alt['group']]  # open again after the withdrawal
    assert ws.choose(pid, alt['group'], 'B')['promoted']


def test_duplicate_or_failed_alternatives_are_not_compared(ws):
    pid = ab_project(ws, ['同一题'], ['同一题'])
    gen = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)
    assert gen['alternatives'] == [] and '几乎相同' in gen['ab_note']
    assert ws.questions(pid)[0]['status'] == 'draft' and ws.overview(pid)['pending_ab'] == []

    def flaky(role, payload, system):
        if '优先设计贴近真实情境' in system:
            raise RuntimeError('超时')
        return {'questions': [question('另一道题')]}

    ws.llm.handlers['generate_questions'] = flaky
    gen = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)
    assert gen['alternatives'] == [] and '没有生成成功' in gen['ab_note']


def test_sampling_is_decided_per_objective(tmp_path):
    settings = load_settings(db=str(tmp_path / 'e.sqlite'), audit_rate=0.0, ab_rate=0.5, concurrency=1)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=ScriptedRng([0.1]))
    pid = ab_project(ws, ['题一', '题二', '题三', '题四'], ['替一'])
    gen = ws.generate_questions(pid, types=['calculation'], count=3)
    assert len(gen['alternatives']) == 1  # one draw decides the whole objective
    assert gen['alternatives'][0]['paired_a_index'] in (0, 1, 2)
    ws.rng = ScriptedRng([0.9])
    assert ws.generate_questions(pid, types=['calculation'], count=1)['alternatives'] == []


def test_sampled_alternatives_have_no_daily_cap(tmp_path):
    settings = load_settings(db=str(tmp_path / 'e.sqlite'), audit_rate=0.0, ab_rate=1.0, concurrency=1)
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(3))
    pid = ab_project(ws, [f'题{i}' for i in range(8)], [f'替{i}' for i in range(8)])
    for _ in range(3):
        assert ws.generate_questions(pid, types=['calculation'])['alternatives']


def test_ab_arms_share_the_input_and_b_has_no_original(ws):
    pid = ab_project(ws, ['原题'], ['替代'])
    ws.generate_questions(pid, types=['calculation'], count=1, ab=True)
    calls = [c for c in ws.llm.calls if c['task'] == 'generate_questions']
    a = next(c for c in calls if '优先设计贴近真实情境' not in c['system'])
    b = next(c for c in calls if '优先设计贴近真实情境' in c['system'])
    assert a['user'] == b['user']  # the same task input; only the system prompt differs
    assert '原题' not in b['user'] and '替代要求' not in b['user']


def test_revoking_an_adoption_restores_the_compared_question(ws):
    pid = ab_project(ws, ['原题一'], ['替代一'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)['alternatives']
    out = ws.choose(pid, alt['group'], 'B')
    assert [q['content']['stem'] for q in ws.questions(pid) if q['status'] == 'draft'] == ['替代一']
    ws.revoke_choice(out['choice_id'])
    assert [q['content']['stem'] for q in ws.questions(pid) if q['status'] == 'draft'] == ['原题一']
    assert ws.overview(pid)['pending_ab'] == [alt['group']]


def test_merging_uses_the_original_and_its_alternative(ws):
    pid = ab_project(ws, ['原题'], ['替代'])
    [alt] = ws.generate_questions(pid, types=['calculation'], count=1, ab=True)['alternatives']
    merged = ws.merge_candidates(
        pid,
        alt['group'],
        {'type': 'calculation', 'stem': '结合两份：原题和替代'},
        {'points': [{'description': '结果', 'score': 2}]},
        [alt['question'], alt['alternative']],
    )
    assert merged['origin'] == 'merged' and {r['role'] for r in merged['refs']} == {'merged_from'}


def test_generation_returns_before_the_checks_finish(tmp_path):
    import time

    settings = load_settings(
        db=str(tmp_path / 'e.sqlite'), audit_rate=0.0, ab_rate=1.0, concurrency=2, background_checks=True
    )
    ws = Workspace(Store(settings.db), FakeLLM(dict(HANDLERS)), settings, rng=random.Random(1))
    pid = ab_project(ws, ['原题'], ['替代'])
    slow = HANDLERS['review_question']

    def review(r, payload):
        time.sleep(0.2)  # keeps the background work from finishing before the assertions below
        return slow(r, payload)

    ws.llm.handlers['review_question'] = review
    gen = ws.generate_questions(pid, types=['calculation'], count=1)
    # the questions come back now; the comparison is published with the checks
    assert gen['background']['run'] and gen['alternatives'] == []
    [q] = [x for x in ws.questions(pid) if x['status'] == 'draft']
    assert not (q['content'].get('checks') or {}).get('solver')
    for _ in range(500):
        with ws.store.read() as c:
            r = c.execute('SELECT status FROM runs WHERE id=?', (gen['background']['run'],)).fetchone()
        if r and r['status'] != 'running':
            break
        time.sleep(0.02)
    assert r['status'] == 'succeeded'
    [q] = [x for x in ws.questions(pid) if x['status'] == 'draft']
    assert q['content']['checks']['solver'] == 'agree'  # review, solve and agreement landed on the question
    assert len(ws.overview(pid)['pending_ab']) == 1  # both versions passed, so the comparison is open


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
    assert new['revision'] == {
        'target': 'activity:1',
        'label': '活动 1',
        'instruction': '缩短导入活动',
        'changed': ['activities'],
    }
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
    sent = ws.llm.last('draft_lesson_plan')['user']['参考材料']
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


# ----- source groups, research cases, consent and export -----


def test_cases_need_independent_review_and_splits_follow_source_groups(ws):
    crs, _o3, _o5, _pid, _q, a3 = analysed_class(ws)
    ws.build_research_cases(crs['id'])
    cases = {(x['stage'], x['scope']): x for x in ws.research_cases(crs['id'])}
    assert {('lesson_plan', ''), (('questions', 'round1')), ('grading', a3['id']), ('analysis', a3['id'])} <= set(cases)
    groups = {x['source_group'] for x in cases.values()}
    assert len(groups) == 1  # plan, questions, gradings and analysis share one source
    case = cases[('grading', a3['id'])]
    with pytest.raises(Conflict):
        ws.review_case(case['id'], 'teacher', 'approve', '逐点核对')  # the producer cannot review
    ws.review_case(case['id'], '审定人甲', 'approve', '逐点核对评分要点')
    assert ws.review_case(case['id'], '审定人乙', 'reject', '逐点核对评分要点')['status'] == 'disputed'
    assert ws.review_case(case['id'], '教研组长', 'approve', '复议', role='adjudicator')['status'] == 'approved'
    ws.freeze_split(crs['id'], test_ratio=0.999)
    splits = {x['split'] for x in ws.research_cases(crs['id'])}
    assert splits == {'test'}  # one source group, one split
    with pytest.raises(Conflict):
        ws.use_for_tuning([case['id']], '调批改提示词')
    check = ws.check_split({cases[('lesson_plan', '')]['id']: 'dev', case['id']: 'test'})
    assert check['crossing_groups'] == [case['source_group']]


def test_training_export_needs_consent_and_keeps_student_text_out(ws):
    crs, _o3, _o5, _pid, _q, _a3 = analysed_class(ws)
    first = ws.export_dataset(crs['id'])
    assert first['records'] == [] and first['manifest']['excluded']['no_training_consent'] >= 2
    consent = ws.grant_consent(crs['id'], ['training'], 'v1', '张老师')
    data = ws.export_dataset(crs['id'])
    m = data['manifest']
    assert m['counts']['demonstration'] == 2 and m['counts']['preference'] == 0  # plan and question
    assert m['excluded']['student_text'] >= 1 and m['excluded']['verification_student_text'] >= 1
    blob = json.dumps(data['records'], ensure_ascii=False)
    assert MISSED not in blob and '乙' not in blob
    assert {r['split'] for r in data['records']} <= {'dev', 'test'} and all(
        r['consent']['version'] == 'v1' for r in data['records']
    )
    marked = ws.revoke_consent(consent['id'])['datasets_marked']
    assert len(marked) == 1
    assert ws.export_dataset(crs['id'])['records'] == []


def test_reviews_are_bound_to_case_version(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    ws.build_research_cases(crs['id'])
    case = next(c for c in ws.research_cases(crs['id']) if c['stage'] == 'lesson_plan')
    ws.review_case(case['id'], '复议人', 'approve', '旧标准', role='adjudicator')
    plan = ws.lesson_plans(pid)[0]
    ws.confirm(ws.revise_plan_part(plan['id'], 'activity:1', '缩短导入')['lesson_plan']['id'])
    ws.build_research_cases(crs['id'])
    updated = next(c for c in ws.research_cases(crs['id']) if c['id'] == case['id'])
    assert updated['version'] == case['version'] + 1 and updated['status'] == 'changed'
    with pytest.raises(Conflict, match='内容已变化'):
        ws.review_case(case['id'], '新审定人', 'approve', '标准', expected_version=case['version'])
    assert ws.review_case(case['id'], '新审定人', 'reject', '新版不合格')['status'] == 'rejected'
    assert ws.review_case(case['id'], '复议人', 'reject', '新版标准', role='adjudicator')['status'] == 'rejected'
    ws.build_research_cases(crs['id'])
    assert next(c for c in ws.research_cases(crs['id']) if c['id'] == case['id'])['version'] == updated['version']


def test_edit_export_waits_for_adoption_and_uses_final_teacher_version(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    ws.grant_consent(crs['id'], ['training'], 'v1', '老师')
    plan = ws.lesson_plans(pid)[0]
    revision = ws.revise_plan_part(plan['id'], 'activity:1', '缩短导入')['lesson_plan']
    assert not [r for r in ws.export_dataset(crs['id'])['records'] if r['type'] == 'edit']
    final = ws.edit(revision['id'], {'notes': '老师核对后的最终稿'})
    ws.confirm(final['id'])
    [record] = [r for r in ws.export_dataset(crs['id'])['records'] if r['type'] == 'edit']
    assert record['target']['notes'] == '老师核对后的最终稿' and final['id'] in record['origin_ids']


def test_tuning_before_freeze_and_new_descendants_stay_in_dev(ws):
    crs, o3, _ = two_classes(ws)
    pid, _ = prep_and_practice(ws, crs, o3)
    ws.build_research_cases(crs['id'])
    case = ws.research_cases(crs['id'])[0]
    ws.use_for_tuning([case['id']], '调提示词')
    ws.freeze_split(crs['id'], test_ratio=0.999999, seed='would-be-test')
    assert {c['split'] for c in ws.research_cases(crs['id'])} == {'dev'}
    ws.grant_consent(crs['id'], ['training'], 'v1', '老师')
    plan = ws.lesson_plans(pid)[0]
    ws.confirm(ws.revise_plan_part(plan['id'], 'activity:1', '缩短导入')['lesson_plan']['id'])
    ws.build_research_cases(crs['id'])
    data = ws.export_dataset(crs['id'], test_ratio=0.999999, seed='another')
    assert {r['split'] for r in data['records']} == {'dev'}


def test_frozen_exports_ignore_new_seed_and_version_changes_with_consent(ws):
    crs, o3, _ = two_classes(ws)
    prep_and_practice(ws, crs, o3)
    ws.grant_consent(crs['id'], ['training'], 'v1', '老师')
    first = ws.export_dataset(crs['id'], test_ratio=0.999999)
    second = ws.export_dataset(crs['id'], seed='new', test_ratio=0.000001)
    assert first['records'] == second['records']
    assert first['manifest']['version'] == second['manifest']['version']
    ws.build_research_cases(crs['id'])
    with pytest.raises(Conflict, match='调优'):
        ws.use_for_tuning([ws.research_cases(crs['id'])[0]['id']], '调提示词')
    ws.grant_consent(crs['id'], ['training'], 'v2', '老师')
    assert ws.export_dataset(crs['id'])['manifest']['version'] != first['manifest']['version']


def test_source_family_partition_conflicts_fail_closed(ws):
    from edu_core import artifacts as A

    crs, o3, _ = two_classes(ws)
    _pid, q = prep_and_practice(ws, crs, o3)
    other = ws.create_course('另一门课', '数学')
    pid2 = ws.start_task(other['id'], 'prep', knowledge_module='另一主题')['project']['id']
    plan2 = ws.draft_lesson_plan(pid2)['lesson_plan']
    ws.confirm(plan2['id'])
    for course in (crs, other):
        ws.grant_consent(course['id'], ['training'], 'v1', '老师')
    ws.export_dataset(crs['id'], test_ratio=0.999999)
    ws.export_dataset(other['id'], test_ratio=0.000001)
    # A new derived artifact joins two previously separated source families.
    with ws.store.tx() as c:
        A.create(
            c,
            pid2,
            'lesson_plan',
            plan2['content'],
            origin='merged',
            refs=[(q['id'], 'derived_from'), (plan2['id'], 'derived_from')],
        )
    all_groups = ws.source_groups()['artifacts']
    assert ws.source_groups(other['id'])['artifacts'][plan2['id']] == all_groups[q['id']]
    with pytest.raises(Conflict, match='历史冲突'):
        ws.export_dataset(other['id'])


def test_legacy_tuning_history_is_honored_before_freezing(ws):
    from edu_core.storage import event

    crs, o3, _ = two_classes(ws)
    prep_and_practice(ws, crs, o3)
    ws.build_research_cases(crs['id'])
    case = ws.research_cases(crs['id'])[0]
    with ws.store.tx() as c:
        event(c, 'research', 'case_used_for_tuning', {'case': case['id'], 'purpose': '旧版本调优记录'})
    ws.freeze_split(crs['id'], test_ratio=1)
    assert {c['split'] for c in ws.research_cases(crs['id'])} == {'dev'}


def test_old_reviews_are_retained_but_need_revalidation(tmp_path, monkeypatch):
    from edu_core import storage

    db = str(tmp_path / 'old.sqlite')
    with monkeypatch.context() as m:
        m.setattr(storage, 'SCHEMA_VERSION', 8)
        old = storage.Store(db)
    with old.tx() as c:
        c.execute(
            "INSERT INTO research_cases(id,project_id,stage,artifact_ids,source_group,status,created_at) VALUES('case1','p','lesson_plan','[]','g','approved',0)"
        )
        c.execute(
            "INSERT INTO case_reviews(id,case_id,reviewer,role,verdict,standard,created_at) VALUES('review1','case1','reviewer','adjudicator','approve','standard',0)"
        )
    upgraded = storage.Store(db)
    storage.Store(db)  # Reopening is safe and does not invalidate newly versioned data again.
    with upgraded.read() as c:
        case = dict(c.execute('SELECT * FROM research_cases').fetchone())
        review = dict(c.execute('SELECT * FROM case_reviews').fetchone())
    assert case['status'] == 'changed' and case['version'] == 1
    assert review['case_version'] == 0 and review['verdict'] == 'approve'
