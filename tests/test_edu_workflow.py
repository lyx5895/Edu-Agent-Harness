"""Full teacher loop with a scripted model: lesson plan -> questions -> grading -> analysis -> practice -> comparison."""

import random

import pytest
from edu_fake import FakeLLM, judge_points

from edu_core.config import load_settings
from edu_core.context import ContextViolation
from edu_core.storage import Store
from edu_core.workflow import Conflict, Workspace

RULES = {'P1': ('x>1', '条件遗漏（定义域、取值范围等）'), 'P2': ('1<x<9', '结论缺失'), 'P3': ('x', '表述不完整')}


def question(stem, target=-1):
    return {
        'type': 'calculation',
        'stem': stem,
        'options': [],
        'answer_key': '',
        'accepted': [],
        'reference': '由 x-1>0 得 x>1；log2 单调递增，x-1<8 得 x<9；所以 1<x<9',
        'objective_index': 0,
        'target_pattern_index': target,
        'points': [
            {'description': '写出定义域 x>1', 'score': 2, 'check_kind': 'none', 'check_expected': ''},
            {'description': '结论 1<x<9', 'score': 2, 'check_kind': 'none', 'check_expected': ''},
        ],
    }


def analyze(role, payload):
    first = payload['错误汇总'][0]['证据'][0]
    return {
        'summary': '部分学生遗漏定义域。',
        'patterns': [
            {
                'name': '解对数不等式时遗漏定义域',
                'description': '直接由单调性去掉对数，没有先写真数大于零。',
                'tags': ['条件遗漏（定义域、取值范围等）'],
                'evidence': [
                    {'eid': first['eid'], 'quote': first['学生原文'][:12]},
                    {'eid': 'E999', 'quote': '编造的证据'},
                ],
                'existing_pattern': '',
            },
            {'name': '没有证据的结论', 'description': 'x', 'tags': [], 'evidence': [], 'existing_pattern': ''},
        ],
        'insufficient': [{'question': '请学生解释为什么要写 x>1', 'why': '分不清是遗漏还是不理解'}],
    }


HANDLERS = {
    'generate_questions': lambda r, p: {'questions': [question('解不等式 log2(x-1) < 3')]},
    'practice': lambda r, p: {'questions': [question('解不等式 log3(x-2) < 2（巩固）', target=0)]},
    'self_check_solve': lambda r, p: {'answer': '定义域 x>1；单调递增得 x-1<8；所以 1<x<9', 'final_answer': '1<x<9'},
    'grade': judge_points(RULES),
    'review': judge_points(RULES),
    'analyze': analyze,
    'draft_lesson_plan': lambda r, p: {
        'objectives': [{'text': '能在解对数不等式时检查定义域', 'success_criterion': '写出真数大于零'}],
        'key_points': ['真数大于零'],
        'difficulties': ['取交集'],
        'activities': [
            {'name': '复习定义域', 'minutes': 5, 'description': '口答', 'objective_index': 0},
            {'name': '例题', 'minutes': 15, 'description': '板演', 'objective_index': 9},
        ],
        'notes': '',
    },
    'draft_review': lambda r, p: {
        'focus': ['先写定义域'],
        'sections': [
            {
                'pattern_index': 0,
                'title': '别忘了真数大于零',
                'key_steps': ['写出 x-1>0', '利用单调性', '取交集'],
                'typical_quote': p['问题模式'][0]['学生原文'][0][:8],
                'error_analysis': '直接去掉对数，漏了定义域。',
                'follow_up': ['为什么要先写 x-1>0？'],
            },
            {
                'pattern_index': 0,
                'title': '编造的引用',
                'key_steps': [],
                'typical_quote': '学生根本没写过这句',
                'error_analysis': '…',
                'follow_up': [],
            },
            {
                'pattern_index': 7,
                'title': '不存在的模式',
                'key_steps': [],
                'typical_quote': '',
                'error_analysis': '',
                'follow_up': [],
            },
        ],
        'closing': '下节课再练。',
    },
}


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    llm = FakeLLM(dict(HANDLERS))  # tests mutate handlers; never share the module-level dict
    return Workspace(Store(settings.db), llm, settings, rng=random.Random(1))


def setup_round_one(ws):
    p = ws.create_project('高一数学｜对数函数｜第2课时', '数学', grade='高一', knowledge_module='对数函数')
    ws.add_material(p['id'], '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    plan = ws.draft_lesson_plan(p['id'])
    assert plan['context_note'] == '已参考：项目背景、材料 1 份'
    ws.confirm(plan['lesson_plan']['id'])
    gen = ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False)
    [q] = gen['candidates'][0]['questions']
    assert q['content']['checks']['solver'] == 'agree' and not q['content']['checks']['answer_suspect']
    assert '备课方案第 1 版（已确认）' in gen['context_note']
    assert q['content']['objective_ids'] == ['O1']
    ws.confirm(q['id'])
    ws.add_learners(p['id'], ['甲', '乙', '丙'])
    return p, q


def test_full_loop(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    imported = ws.import_submissions(
        pid, '学生,题号,作答\n甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9\n丁,1,不知道'
    )
    assert imported == {'created': 2, 'unmatched_learners': ['丁'], 'problems': []}

    graded = ws.grade_round(pid)
    assert graded['graded'] == 2 and graded['failed'] == []
    by_learner = {g['content']['learner_alias']: g for g in ws.gradings(pid)}
    assert by_learner['甲']['content']['total'] == 4
    assert by_learner['乙']['content']['points'][0]['error_tag'] == '条件遗漏（定义域、取值范围等）'
    assert ws.review_queue(pid) == []
    assert ws.grade_round(pid)['graded'] == 0  # nothing changed: no regrade

    with pytest.raises(Conflict):
        ws.analyze(pid)  # gradings must be confirmed first
    assert ws.confirm_gradings(pid) == {'confirmed': 2}

    out = ws.analyze(pid)
    [pattern] = out['patterns']  # the evidence-free pattern is dropped
    assert [e['learner'] for e in pattern['content']['evidence']] == ['乙']  # fabricated eid dropped
    assert pattern['content']['affected_learners'] == ['乙'] and pattern['content']['total_learners'] == 2
    assert out['analysis']['content']['insufficient'][0]['question'].startswith('请学生解释')
    assert out['analysis']['content']['dropped_patterns'] == ['没有证据的结论']
    ws.confirm(pattern['id'])

    prac = ws.practice(pid)
    assert prac['round'] == 2
    [pq] = prac['questions']
    assert pq['content']['target_patterns'] == [pattern['lineage_id']]
    assert all(pt['target_pattern'] == pattern['lineage_id'] for pt in pq['rubric']['content']['points'])
    ws.confirm(pq['id'])
    ws.import_submissions(pid, '甲,1,定义域 x>2，所以 2<x<11\n乙,1,定义域 x>2；得 2<x<11', round_=2)
    ws.grade_round(pid, round_=2)
    ws.confirm_gradings(pid, round_=2)
    cmp = ws.compare(pid, 1, 2)
    row = cmp['content']['patterns'][0]
    assert row['retested'] == ['乙', '甲'] or set(row['retested']) == {'甲', '乙'}

    # A substantive rubric change makes round-1 gradings and the analysis stale; regrading clears it.
    rubric = ws.questions(pid, round_=1)[0]['rubric']
    new = ws.edit(
        rubric['id'], {'points': [*rubric['content']['points'], {'id': 'P3', 'description': '书写规范', 'score': 1}]}
    )
    ws.confirm(new['id'], change_kind='substantive')
    stale = ws.stale(pid)
    stale_types = sorted(m['type'] for m in stale)
    assert stale_types.count('grading') == 2 and {m['round'] for m in stale if m['type'] == 'grading'} == {1}
    assert {'analysis', 'problem_pattern', 'comparison'} <= set(stale_types)
    assert 'submission' not in stale_types
    # The practice question built on the pattern is flagged for review, but its round-2 gradings are not.
    assert any(m['type'] == 'question' and m['round'] == 2 for m in stale)
    regraded = ws.grade_round(pid)
    assert regraded['graded'] == 2 and not [m for m in ws.stale(pid) if m['type'] == 'grading']

    overview = ws.overview(pid)
    assert overview['stale'] and overview['counts']['grading']['total'] >= 2

    export = ws.export(pid)
    counts = export['manifest']['counts']
    assert counts['material_families'] == 1 and counts['course_projects'] == 1
    assert counts['model_responses'] == len(export['generations']) > 0
    assert any(e['changed_fields'] == ['points', 'total'] for e in export['edits'])
    assert all(g['context_manifest'] for g in export['generations'])


def test_teacher_override_and_queue(ws):
    p, _ = setup_round_one(ws)
    pid = p['id']
    lenient = judge_points({'P1': ('x', ''), 'P2': ('x', '')})
    ws.llm.handlers['review'] = lenient
    ws.import_submissions(pid, '乙,1,由单调性 x-1<8，所以 x<9')
    ws.grade_round(pid)
    [item] = ws.review_queue(pid)
    assert 'review_disagreement' in item['content']['risks'] and item['answer'].startswith('由单调性')
    with pytest.raises(Conflict):
        ws.confirm_gradings(pid, ids=[item['id']])  # undetermined points must be graded by the teacher
    with pytest.raises(ValueError):
        ws.override(item['id'], [{'id': 'P1', 'score': 0}])  # P2 still undetermined
    done = ws.override(
        item['id'], [{'id': 'P1', 'score': 0, 'error_tag': '条件遗漏（定义域、取值范围等）'}, {'id': 'P2', 'score': 1}]
    )
    assert done['status'] == 'confirmed' and done['content']['total'] == 1 and done['origin'] == 'teacher'
    assert ws.review_queue(pid) == []


def test_ab_candidates_and_choice(ws):
    p = ws.create_project('物理｜匀速直线运动', 'physics', knowledge_module='匀速直线运动')
    ws.confirm(ws.draft_lesson_plan(p['id'])['lesson_plan']['id'])  # course info alone is enough
    gen = ws.generate_questions(p['id'], types=['calculation'], ab=True)
    assert gen['candidate_group'] and [c['position'] for c in gen['candidates']] == ['A', 'B']
    assert {c['variant'] for c in gen['candidates']} == {'a', 'b'}
    assert ws.questions(p['id'])[0]['status'] == 'candidate'
    out = ws.choose(p['id'], gen['candidate_group'], 'B', reason='更贴近生活')
    assert len(out['promoted']) == 2  # question + rubric
    statuses = sorted(q['status'] for q in ws.questions(p['id']))
    assert statuses == ['candidate', 'draft']  # the unchosen candidate stays as a research record
    with pytest.raises(Conflict):
        ws.choose(p['id'], gen['candidate_group'], 'A')


def test_pitfalls_without_submissions(ws):
    ws.llm.handlers['pitfalls'] = lambda r, p: {
        'pitfalls': [{'name': '遗漏定义域', 'description': '可能直接去对数', 'question_nos': [1]}]
    }
    p, _ = setup_round_one(ws)
    out = ws.analyze(p['id'])
    assert out['analysis']['content']['mode'] == 'pitfalls' and out['patterns'] == []
    assert '未基于学生作答' in out['analysis']['content']['note']


def test_questions_need_a_confirmed_lesson_plan(ws):
    p = ws.create_project('历史｜辛亥革命', 'history')
    with pytest.raises(Conflict):
        ws.generate_questions(p['id'])
    with pytest.raises(ContextViolation):
        from edu_core.context import Context

        Context('generate_questions').add_artifact({'type': 'grading', 'status': 'confirmed', 'version': 1})


def test_usable_drafts_fill_keyed_points_and_report_dropped():
    from edu_core.workflow import QuestionDraft, usable_drafts

    base = question('x')
    mcq = QuestionDraft(**{**base, 'type': 'single_choice', 'answer_key': 'B', 'points': []})
    essay = QuestionDraft(**{**base, 'type': 'short_answer', 'points': []})
    off_target = QuestionDraft(**{**base, 'target_pattern_index': 5})
    kept, dropped = usable_drafts([mcq, essay], {'single_choice', 'short_answer'})
    assert [d.type for d in kept] == ['single_choice'] and kept[0].points[0].score == 2
    assert len(dropped) == 1 and '缺少评分要点' in dropped[0]
    _, dropped = usable_drafts([off_target], {'calculation'}, n_patterns=2)
    assert '问题模式' in dropped[0]


def test_self_check_calls_are_recorded(ws):
    p, q = setup_round_one(ws)
    tasks = [g['task'] for g in ws.export(p['id'])['generations']]
    assert 'self_check_solve' in tasks and 'grade' in tasks
    assert len(q['content']['checks']['generation_ids']) == 2


def test_next_step_guides_through_the_workflow(ws):
    p = ws.create_project('高一数学', 'chinese')
    pid = p['id']
    [step] = ws.overview(pid)['next']
    assert (step['label'], step['tab'], step['target']) == ('填写课时信息', 'prep', '#course-module')
    with pytest.raises(Conflict):
        ws.draft_lesson_plan(pid)  # nothing to plan from yet
    ws.update_project(pid, knowledge_module='对数函数', lesson='第2课时', learner_profile='刚学完指数函数')
    assert ws.overview(pid)['next'][0]['target'] == '#draft-lesson-plan'  # material is optional
    plan = ws.draft_lesson_plan(pid)['lesson_plan']
    assert (
        ws.llm.calls[-1]['user']['知识模块'] == '对数函数' and ws.llm.calls[-1]['user']['学生基础'] == '刚学完指数函数'
    )
    assert ws.overview(pid)['next'][0]['label'] == '确认备课方案'
    with pytest.raises(Conflict):
        ws.generate_questions(pid)  # the plan is still a draft
    ws.confirm(plan['id'])
    steps = ws.overview(pid)['next']
    assert [x['target'] for x in steps] == ['#generate-questions']
    assert ws.overview(pid)['stages']['prep']

    # The subject was picked wrong; it can still change because there is no question yet.
    assert ws.update_project(pid, subject='数学')['subject'] == 'math'
    q = ws.generate_questions(pid, types=['calculation'], count=1, ab=False)['candidates'][0]['questions'][0]
    assert ws.overview(pid)['next'][0]['label'] == '确认题目'
    with pytest.raises(Conflict):
        ws.update_project(pid, subject='physics')
    assert ws.update_project(pid, title='高一数学｜对数函数')['title'] == '高一数学｜对数函数'
    ws.confirm(q['id'])
    assert ws.overview(pid)['next'][0]['target'] == '#roster'
    ws.add_learners(pid, ['S01'])
    assert ws.overview(pid)['next'][0]['target'] == '#sub-csv'
    ws.import_submissions(pid, 'S01,1,定义域 x>1，又 x-1<8，所以 1<x<9')
    assert ws.overview(pid)['next'][0]['target'] == '#grade-round'
    ws.grade_round(pid)
    assert ws.overview(pid)['next'][0]['target'] == '#confirm-auto'  # graded but not confirmed yet
    ws.confirm_gradings(pid)
    assert ws.overview(pid)['next'][0]['target'] == '#analyze'


def test_lesson_plan_review_and_second_round(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    with ws.store.read() as c:
        plan = ws._plan(c, pid)
    acts = plan['content']['activities']
    assert acts[0]['objective_ids'] == ['O1'] and acts[1]['objective_ids'] == []  # out-of-range index dropped
    assert plan['content']['key_points'] == ['真数大于零']

    ws.import_submissions(pid, '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    out = ws.analyze(pid)
    assert ws.overview(pid)['next'][0]['label'] == '确认问题模式'
    with pytest.raises(Conflict):
        ws.draft_review(pid)  # patterns must be confirmed first
    pattern = out['patterns'][0]
    ws.confirm(pattern['id'])
    assert ws.overview(pid)['next'][0] == {
        'label': '定位讲解重点，准备讲评',
        'tab': 'review',
        'target': '#draft-review',
        'hint': '',
    }

    review = ws.draft_review(pid)['review_plan']
    sections = review['content']['sections']
    assert [x['title'] for x in sections] == ['别忘了真数大于零', '编造的引用']  # unknown pattern dropped
    assert sections[0]['typical_quote'] and sections[0]['typical_source']['question_no'] == 1  # verified quote
    assert sections[1]['typical_quote'] == '' and sections[1]['quote_rejected']  # fabricated quote removed
    assert review['content']['dropped'] == ['不存在的模式']
    assert ws.overview(pid)['next'][0]['label'] == '确认讲评稿'
    ws.confirm(review['id'])
    assert ws.overview(pid)['next'][0]['target'] == '#practice'

    # 讲评后再练 → back to 出题改题 as round 2, with the confirmed review focus in context
    prac = ws.practice(pid)
    assert prac['round'] == 2 and '讲评稿第 1 版（已确认）' in prac['context_note']
    assert any(c['task'] == 'practice' and c['user']['讲评重点'] == ['先写定义域'] for c in ws.llm.calls)
    step = ws.overview(pid)['next'][0]
    assert step['label'] == '确认题目（第 2 轮）' and step['tab'] == 'questions'
    ws.confirm(prac['questions'][0]['id'])
    assert ws.overview(pid)['next'][0]['label'] == '导入学生作答（第 2 轮）'
    ws.import_submissions(pid, '甲,1,定义域 x>2，所以 2<x<11\n乙,1,x>2 所以 2<x<11', round_=2)
    ws.grade_round(pid, round_=2)
    ws.confirm_gradings(pid, round_=2)
    assert ws.overview(pid)['next'][0]['label'] == '汇总批改结果（第 2 轮）'
    ws.analyze(pid, round_=2)  # round 2 reuses or adds patterns
    labels = [x['label'] for x in ws.overview(pid)['next']]
    assert labels[0] == '对比第 1、2 轮'
    cmp = ws.compare(pid, 1, 2)  # still finds round-1 pattern versions after round 2 analysis
    assert cmp['content']['patterns'][0]['name'] == '解对数不等式时遗漏定义域'


def test_plan_is_one_artifact_and_edits_keep_objective_ids(ws):
    p, q = setup_round_one(ws)
    pid = p['id']
    with ws.store.read() as c:
        plan = ws._plan(c, pid)
    content = plan['content']
    edited = {
        **content,
        'objectives': [
            {**content['objectives'][0], 'text': '能先写出真数大于零再求解'},
            {'text': '能用区间表示解集', 'success_criterion': '写成 (1,9)'},
        ],
        'key_points': [*content['key_points'], '区间表示'],
    }
    draft = ws.edit(plan['id'], edited)
    assert [o['id'] for o in draft['content']['objectives']] == ['O1', 'O2']  # ids survive edits; new one appended
    with pytest.raises(ValueError):
        ws.edit(plan['id'], {**content, 'objectives': []})
    ws.confirm(draft['id'], change_kind='substantive')
    flagged = {m['artifact_id'] for m in ws.stale(pid)}
    assert q['id'] in flagged  # questions built on the old plan are flagged for review


def test_import_paper_merges_inferred_objectives_into_the_plan(ws):
    p, _q = setup_round_one(ws)  # plan has O1 "能在解对数不等式时检查定义域"
    pid = p['id']
    paper = question('解不等式 log2(x+1) > 1')
    ws.llm.handlers['import_paper'] = lambda r, payload: {
        'objectives': [
            {'text': '能在解对数不等式时检查定义域', 'success_criterion': '同上'},  # already in the plan
            {'text': '能判断底数对单调性的影响', 'success_criterion': '分类讨论'},
        ],
        'questions': [{**paper, 'objective_index': 1}],
    }
    out = ws.import_paper(pid, '1. 解不等式 log2(x+1) > 1。')
    assert ws.llm.calls[-1]['user']['已有教学目标'] == ['能在解对数不等式时检查定义域']
    objectives = out['lesson_plan']['content']['objectives']
    assert [o['id'] for o in objectives] == ['O1', 'O2'] and objectives[1]['inferred']  # duplicate not re-added
    assert out['lesson_plan']['status'] == 'draft' and out['lesson_plan']['version'] == 2
    assert out['questions'][0]['content']['objective_ids'] == ['O2']
