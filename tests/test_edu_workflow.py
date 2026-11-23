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
    # the best lexical candidates, like the automatic pick
    'check_plan': lambda r, p: {'issues': []},
    'pick_standards': lambda r, p: {'ids': [c['编号'] for c in p['候选条目'][:3]], 'note': ''},
    'infer_objectives': lambda r, p: {
        'objectives': [{'text': f'能用{p.get("主题") or "所学内容"}解决基础问题', 'success_criterion': '写出关键步骤'}]
    },
    'practice': lambda r, p: {'questions': [question('解不等式 log3(x-2) < 2（巩固）', target=0)]},
    'self_check_solve': lambda r, p: {'answer': '定义域 x>1；单调递增得 x-1<8；所以 1<x<9', 'final_answer': '1<x<9'},
    'self_check_compare': lambda r, p: {'consistent': True, 'note': ''},
    'review_question': lambda r, p: {
        'tests_objective': True,
        'objective_note': '',
        'in_scope': True,
        'scope_note': '',
        'difficulty': p.get('要求难度') or '中等',
        'issues': [],
    },
    'grade': judge_points(RULES),
    'review': judge_points(RULES),
    'analyze': analyze,
    'draft_lesson_plan': lambda r, p: {
        'objectives': [
            {
                'text': '能在解对数不等式时检查定义域',
                'success_criterion': '写出真数大于零',
                # cite the first standard item offered, plus one the model made up
                'standard_ids': [*[x['编号'] for x in p.get('课程标准', {}).get('内容要求', [])][:1], 'math.X9.9.9'],
                'beyond_standard': False,
                'beyond_reason': '',
            }
        ],
        'key_points': ['真数大于零'],
        'difficulties': ['取交集'],
        'activities': [
            {
                'name': '复习定义域',
                'minutes': 5,
                'description': '口答',
                'objective_index': 0,
                'evidence': '学习单第 1 题，检验目标 1',
            },
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
    assert plan['context_note'] == '已参考：课时背景、材料 1 份、课程标准 3 条'
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
        ws.analyze(pid, partial=True)  # gradings must be confirmed first
    assert ws.confirm_gradings(pid) == {'confirmed': 2, 'held': 0}

    out = ws.analyze(pid, partial=True)
    assert out['analysis']['content']['partial']
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

    def generate(role, payload):
        # The two arms are independent draws under different prompts; B asks for one question.
        if payload.get('每个目标出题数') == 1:
            return {'questions': [question('小明 10 分钟走了 1.2 km，求平均速度。')]}
        return {'questions': [question('一辆车 2 小时行驶 120 km，求平均速度。')]}

    ws.llm.handlers['generate_questions'] = generate
    gen = ws.generate_questions(p['id'], types=['calculation'], ab=True)
    [alt] = gen['alternatives']
    assert alt['shown_first'] in ('A', 'B') and alt['paired_a_index'] == 0
    assert gen['candidates'][0]['position'] == 'A'
    assert sorted(q['status'] for q in ws.questions(p['id'])) == ['candidate', 'draft']
    out = ws.choose(p['id'], alt['group'], 'B', reason='更贴近生活')
    assert len(out['promoted']) == 2  # the question's next version and its rubric
    assert [q['content']['stem'] for q in ws.questions(p['id'])] == ['小明 10 分钟走了 1.2 km，求平均速度。']
    with pytest.raises(Conflict):
        ws.choose(p['id'], alt['group'], 'A')


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


def test_infer_goals_picks_standards_in_the_same_call(ws):
    p = ws.create_project('对数函数练习', '数学', knowledge_module='对数函数')
    ws.add_material(p['id'], '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    picked = []

    def infer(r, payload):
        assert payload['候选课标条目']
        picked.append(payload['候选课标条目'][0]['编号'])
        return {
            'objectives': [
                {'text': '能解简单对数不等式', 'success_criterion': '写出定义域', 'standard_ids': picked[-1:]}
            ]
        }

    ws.llm.handlers['infer_objectives'] = infer
    before = len(ws.llm.calls)
    ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False)
    calls = ws.llm.calls[before:]
    assert not [c for c in calls if c['task'] == 'pick_standards']  # the standard pick is part of the same call
    [plan] = [x for x in ws.lesson_plans(p['id']) if x['status'] == 'draft']
    [goal] = plan['content']['objectives']
    assert goal['standard_ids'] == picked
    assert ws.curriculum(p['id'])['selected'] == picked


def test_suggest_field_drafts_from_context(ws):
    p = ws.create_project('对数函数', '数学', grade='高一', knowledge_module='对数函数')
    ws.add_material(p['id'], '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    ws.llm.handlers['suggest_field'] = lambda r, payload: {'text': '对数函数的图象与性质'}
    out = ws.suggest_field(p['id'], 'lesson_scope', current='', about='对数函数的图象')
    assert out['text'] == '对数函数的图象与性质'
    assert '材料' in out['context_note']
    sent = ws.llm.last('suggest_field')['user']
    assert sent['当前内容'] == '' and sent['材料摘录'] and sent['针对的内容'] == '对数函数的图象'
    with pytest.raises(ValueError):
        ws.suggest_field(p['id'], 'nope')


def test_usable_drafts_fill_keyed_points_and_report_dropped():
    from edu_core.workflow import QuestionDraft, usable_drafts

    base = question('x')
    mcq = QuestionDraft(
        **{
            **base,
            'type': 'single_choice',
            'options': ['1', '2', '3', '4'],
            'answer_key': 'B',
            'points': [],
        }
    )
    essay = QuestionDraft(**{**base, 'type': 'short_answer', 'points': []})
    off_target = QuestionDraft(**{**base, 'target_pattern_index': 5})
    kept, dropped = usable_drafts([mcq, essay], {'single_choice', 'short_answer'})
    assert [d.type for d in kept] == ['single_choice'] and kept[0].points[0].score == 2
    assert len(dropped) == 1 and '缺少评分要点' in dropped[0]
    _, dropped = usable_drafts([off_target], {'calculation'}, n_patterns=2)
    assert '共性问题' in dropped[0]
    bad_key = QuestionDraft(
        **{**base, 'type': 'single_choice', 'options': ['1', '2'], 'answer_key': 'D', 'points': []}
    )
    _, dropped = usable_drafts([bad_key], {'single_choice'})
    assert '答案键超出选项范围' in dropped[0]


def test_self_check_calls_are_recorded(ws):
    p, q = setup_round_one(ws)
    tasks = [g['task'] for g in ws.export(p['id'])['generations']]
    assert 'self_check_solve' in tasks and 'self_check_compare' in tasks and 'review_question' in tasks
    assert 'grade' not in tasks  # the solver's work is not graded point by point any more
    assert len(q['content']['checks']['generation_ids']) == 3  # review + solve + one agreement judgement
    assert q['content']['checks']['state'] == 'done'


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
        ws.llm.last('draft_lesson_plan')['user']['知识模块'] == '对数函数'
        and ws.llm.last('draft_lesson_plan')['user']['学生基础'] == '刚学完指数函数'
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
    # Some roster/question entries are absent: analyze the received portion explicitly.
    out = ws.analyze(pid, partial=True)
    assert ws.overview(pid)['next'][0]['label'] == '确认共性问题'
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
    ws.analyze(pid, round_=2, partial=True)  # round 2 still contains only part of the roster
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


def test_next_lesson_carries_learning_status_into_the_plan(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    ws.update_project(pid, lesson='第2课时', learner_profile='刚学完指数函数')
    with pytest.raises(Conflict):
        ws.next_lesson(pid)  # no 学情 yet
    ws.import_submissions(pid, '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    # Some roster/question entries are absent: analyze the received portion explicitly.
    out = ws.analyze(pid, partial=True)
    [pattern] = out['patterns']
    ws.confirm(pattern['id'])
    # One pattern failed the evidence check, so the model summary is replaced by what the evidence shows.
    assert out['analysis']['content']['summary_source'] == 'server'
    assert out['analysis']['content']['summary'].startswith('有作答证据的问题：解对数不等式时遗漏定义域')
    assert ws.next_lesson(pid)['carryover']['summary'] == ''  # an unconfirmed analysis is not carried over
    ws.confirm(out['analysis']['id'])

    nxt = ws.next_lesson(pid)
    assert nxt['lesson'] == '第 3 课时' and nxt['title'] == '高一数学｜对数函数｜第 3 课时'
    assert nxt['subject'] == 'math' and nxt['knowledge_module'] == '对数函数'
    assert nxt['learner_profile'] == '刚学完指数函数' and nxt['prev_project_id'] == pid
    carry = nxt['carryover']
    assert carry['patterns'][0]['name'] == '解对数不等式时遗漏定义域'
    assert carry['patterns'][0]['affected'] == '1/2' and carry['patterns'][0]['after_practice'] == '还没有再练结果'
    assert carry['objectives'] == ['能在解对数不等式时检查定义域'] and carry['summary']
    assert ws.overview(pid)['next_lesson']['id'] == nxt['id']
    assert ws.overview(nxt['id'])['learners'] == 3  # roster comes along

    plan = ws.draft_lesson_plan(nxt['id'])  # course info + carried-over 学情, no material needed
    assert plan['context_note'] == '已参考：课时背景、课程标准 3 条、上一课时学情'
    sent = ws.llm.last('draft_lesson_plan')['user']['上一课时学情']
    assert sent['问题模式'][0]['名称'] == '解对数不等式时遗漏定义域' and sent['上一课时'] == '第2课时'
    # A project that was not created as a next lesson never mentions one.
    ws.draft_lesson_plan(pid)
    assert '上一课时学情' not in ws.llm.last('draft_lesson_plan')['user']


def test_plan_is_drafted_against_the_curriculum_standard(ws):
    p = ws.create_project('高一数学', 'math', knowledge_module='对数函数', lesson='第2课时')
    pid = p['id']
    ws.update_project(pid, lesson_scope='对数函数的图像与性质')
    sug = ws.curriculum(pid)
    assert sug['available'] and sug['selected'][0] == 'math.B2.2.3.2'  # 对数函数②: 图象与单调性
    assert sug['chosen_by'] is None  # nothing chosen yet: the automatic pick is used and saved at drafting

    # Before drafting, a model confirms the pick from the candidates (here: only 对数函数②, plus a made-up id).
    ws.llm.handlers['pick_standards'] = lambda r, p: {'ids': ['math.B2.2.3.2', 'math.X9.9.9'], 'note': ''}
    plan = ws.draft_lesson_plan(pid)['lesson_plan']['content']
    picked = next(x for x in ws.llm.calls if x['task'] == 'pick_standards')['user']
    assert 'math.B2.2.3.1' in [x['编号'] for x in picked['候选条目']]  # the unit's other items are offered too
    sent = ws.llm.last('draft_lesson_plan')['user']
    assert sent['本课时内容'] == '对数函数的图像与性质'
    offered = [x['编号'] for x in sent['课程标准']['内容要求']]
    assert offered == ['math.B2.2.3.2'] and sent['课程标准']['教学提示']  # the made-up id never reaches the plan
    [o] = plan['objectives']
    assert o['standard_ids'] == [offered[0]]  # the made-up id is dropped
    assert 'checks' not in o and plan['curriculum'] == {'ids': sorted(offered)}
    assert ws.curriculum(pid)['chosen_by'] == 'model'

    # A requirement above the standard's wording is flagged; the citation survives the edit.
    with ws.store.read() as c:
        draft = ws._plan(c, pid, confirmed=False)
    edited = ws.edit(
        draft['id'], {'objectives': [{'id': 'O1', 'text': '熟练掌握对数函数的单调性', 'success_criterion': 'x'}]}
    )
    [o] = edited['content']['objectives']
    assert o['standard_ids'] == [offered[0]] and o['checks'][0].startswith('要求高于课标：课标为“了解”')
    added = ws.edit(draft['id'], {'objectives': [o, {'text': '能画出 y=log2x 的图象', 'success_criterion': 'y'}]})
    # an objective the teacher adds has no standard and no step collecting its evidence yet
    assert added['content']['objectives'][1]['checks'] == ['未对应课标', '课上没有安排检验这条目标的环节']

    # The teacher's own pick wins and is not reset by later course-info edits; 不参考课标 turns it off.
    ws.update_project(pid, curriculum_ids=['math.B2.2.3.1', 'nope'])
    ws.update_project(pid, knowledge_module='对数')
    assert ws.curriculum(pid)['selected'] == ['math.B2.2.3.1']
    ws.update_project(pid, curriculum_off=True)
    ws.draft_lesson_plan(pid)
    assert '课程标准' not in ws.llm.last('draft_lesson_plan')['user']


def test_auto_pick_follows_the_course_info_and_other_subjects_have_none(ws):
    p = ws.create_project('高一数学', 'math', knowledge_module='对数函数')
    ws.draft_lesson_plan(p['id'])
    ws.update_project(p['id'], knowledge_module='基本不等式')
    assert ws.curriculum(p['id'])['selected'] == ['math.B1.3.2']  # the automatic pick was dropped and redone
    h = ws.create_project('历史｜辛亥革命', 'history', knowledge_module='辛亥革命')
    assert ws.curriculum(h['id'])['selected'] == ['history.B1.1.8']  # 1.8 辛亥革命与中华民国的建立
    ws.draft_lesson_plan(h['id'])
    assert ws.llm.last('draft_lesson_plan')['user']['课程标准']['内容要求'][0]['位置'].endswith(
        '1.8辛亥革命与中华民国的建立'
    )
    # A topic the standard does not cover is not guessed at: candidates are offered, nothing is ticked.
    a = ws.create_project('人工智能', 'ai', knowledge_module='支持向量机 核函数')
    sug = ws.curriculum(a['id'])
    assert sug['available'] and sug['selected'] == []
    ws.draft_lesson_plan(a['id'])
    assert '课程标准' not in ws.llm.last('draft_lesson_plan')['user']


def test_questions_are_reviewed_against_objective_scope_and_difficulty(ws):
    p, q = setup_round_one(ws)
    sent = [c for c in ws.llm.calls if c['task'] == 'review_question'][-1]['user']
    assert sent['教学目标'] == '能在解对数不等式时检查定义域' and sent['要求难度'] == '中等'
    assert sent['课标条目']  # the standard items the objective cites
    assert q['content']['checks']['difficulty'] == '中等' and q['content']['checks']['flags'] == []
    assert q['content']['difficulty_requested'] == '中等' and q['content']['purpose'] == '课堂练习'

    ws.llm.handlers['review_question'] = lambda r, p: {
        'tests_objective': False,
        'objective_note': '只考了单调性',
        'in_scope': False,
        'scope_note': '用到了换底公式',
        'difficulty': '较难',
        'issues': ['条件不足'],
    }
    ws.llm.handlers['revise_question'] = lambda r, p: {'questions': [question('修正后：解不等式 log2(x-1) < 3')]}
    gen = ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False, difficulty='基础')
    checks = gen['candidates'][0]['questions'][0]['content']['checks']
    assert checks['flags'] == ['与目标不符：只考了单调性', '超出范围：用到了换底公式', '条件不足']  # only must-fix
    assert (checks['difficulty'], checks['difficulty_asked']) == ('较难', '基础')  # folded into AI 提示, not a flag
    assert 'suggestions' not in checks
    # the findings became the rewrite remark, and the question now carries them as its next version
    sent = ws.llm.last('revise_question')['user']
    assert '条件不足' in sent['老师的修改意见']
    assert gen['candidates'][0]['questions'][0]['content']['stem'] == '修正后：解不等式 log2(x-1) < 3'

    cov = ws.coverage(p['id'])
    [row] = cov['objectives']
    assert (row['id'], row['total'], row['confirmed']) == ('O1', 2, 1) and row['flagged'] == [2]


def test_adopt_solver_answer_and_regenerate_one_question(ws):
    from edu_core.llm import ModelUnavailable

    p, q = setup_round_one(ws)
    ws.llm.handlers['self_check_solve'] = lambda r, p: {'answer': '直接得 x<9', 'final_answer': 'x<9'}
    ws.llm.handlers['self_check_compare'] = lambda r, p: {'consistent': False, 'note': '漏了定义域'}
    ws.llm.handlers['revise_question'] = lambda r, p: (_ for _ in ()).throw(ModelUnavailable('网关超时'))
    gen = ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False)
    [bad] = gen['candidates'][0]['questions']
    assert bad['content']['checks']['answer_suspect']
    fixed = ws.adopt_solver_answer(bad['id'])
    assert fixed['content']['reference'] == '直接得 x<9' and not fixed['content']['checks']['answer_suspect']
    assert fixed['version'] == 2 and fixed['status'] == 'draft'
    with pytest.raises(Conflict):
        ws.adopt_solver_answer(
            ws.create_question(
                p['id'],
                {'type': 'calculation', 'stem': 's', 'reference': 'r'},
                {'points': [{'description': 'd', 'score': 1}]},
            )['id']
        )

    # 换一道 on the confirmed question: same number, same objective, next version of the same question
    ws.llm.handlers['regenerate_question'] = lambda r, p: {'questions': [question('解不等式 log2(x+1) < 2（更简单）')]}
    out = ws.regenerate_question(q['id'], instruction='更简单一些', difficulty='基础')
    new = out['question']
    sent = ws.llm.calls[[c['task'] for c in ws.llm.calls].index('regenerate_question')]['user']
    assert sent['原题（请换一道，不要重复）'] == q['content']['stem'] and sent['老师的修改要求'] == '更简单一些'
    assert (new['lineage_id'], new['version'], new['status']) == (q['lineage_id'], 2, 'draft')
    assert new['content']['no'] == q['content']['no'] and new['content']['objective_ids'] == ['O1']
    assert new['rubric']['lineage_id'] == q['rubric']['lineage_id'] and new['rubric']['version'] == 2
    current = {x['lineage_id']: x for x in ws.questions(p['id'], round_=1)}[q['lineage_id']]
    assert current['content']['stem'].endswith('（更简单）') and current['rubric']['id'] == new['rubric']['id']
    ws.confirm(new['id'], change_kind='substantive')  # question and rubric confirmed together
    after = {x['lineage_id']: x for x in ws.questions(p['id'], round_=1)}[q['lineage_id']]
    assert after['status'] == 'confirmed' and after['rubric']['status'] == 'confirmed'


def test_adopt_solver_answer_clears_numeric_expectations(ws):
    from edu_core.llm import ModelUnavailable

    p, _q = setup_round_one(ws)
    ws.llm.handlers['self_check_solve'] = lambda r, p: {'answer': 'x<9', 'final_answer': 'x<9'}
    ws.llm.handlers['self_check_compare'] = lambda r, p: {'consistent': False, 'note': '漏了定义域'}
    ws.llm.handlers['revise_question'] = lambda r, p: (_ for _ in ()).throw(ModelUnavailable('跳过重写'))
    gen = ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False)
    [bad] = gen['candidates'][0]['questions']
    assert bad['content']['checks']['answer_suspect']
    # The old grading expectation pointed at the answer the teacher is about to replace.
    ws.edit(
        bad['rubric']['id'],
        {'points': [{'id': 'P1', 'description': '结果', 'score': 2, 'check': {'kind': 'numeric', 'expected': '9'}}]},
    )
    fixed = ws.adopt_solver_answer(bad['id'])
    [cur] = [x for x in ws.questions(p['id'], round_=1) if x['lineage_id'] == bad['lineage_id']]
    assert cur['content']['reference'] == 'x<9' and fixed['version'] == 2
    assert cur['rubric']['content']['points'][0]['id'] == 'P1'
    assert 'check' not in cur['rubric']['content']['points'][0]


def test_regenerate_returns_before_the_check_finishes(ws):
    import time
    from dataclasses import replace

    p, q = setup_round_one(ws)
    ws.settings = replace(ws.settings, background_checks=True)
    ws.llm.handlers['regenerate_question'] = lambda r, p: {
        'questions': [question('换一道：解不等式 log2(x+2) < 2')]
    }
    out = ws.regenerate_question(q['id'], instruction='换一道')
    assert out['background']['run']
    assert out['question']['content']['checks']['state'] == 'pending'
    for _ in range(500):
        with ws.store.read() as c:
            row = c.execute('SELECT status FROM runs WHERE id=?', (out['background']['run'],)).fetchone()
        if row and row['status'] != 'running':
            break
        time.sleep(0.02)
    assert row['status'] == 'succeeded'
    [cur] = [x for x in ws.questions(p['id'], round_=1) if x['lineage_id'] == q['lineage_id']]
    assert cur['content']['checks']['state'] == 'done'
    assert cur['content']['checks']['solver'] == 'agree'


def test_generation_sees_activities_standards_and_carried_over_status(ws):
    p, _q = setup_round_one(ws)
    sent = [c for c in ws.llm.calls if c['task'] == 'generate_questions'][-1]
    user = sent['user']
    assert user['课堂活动'][0] == {'活动': '复习定义域', '用时': '5 分钟', '做法': '口答', '对应目标序号': [0]}
    assert user['课标条目'] and all(x['原文'] for x in user['课标条目'])
    assert '备课备注' in user and '上一课时学情' not in user
    assert '课堂活动' in sent['system'] and '上一课时学情' in sent['system']

    pid = p['id']
    ws.import_submissions(pid, '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    # Some roster/question entries are absent: analyze the received portion explicitly.
    ws.confirm(ws.analyze(pid, partial=True)['patterns'][0]['id'])
    nxt = ws.next_lesson(pid)
    ws.confirm(ws.draft_lesson_plan(nxt['id'])['lesson_plan']['id'])
    gen = ws.generate_questions(nxt['id'], types=['calculation'], count=1, ab=False)
    user = [c for c in ws.llm.calls if c['task'] == 'generate_questions'][-1]['user']
    assert user['上一课时学情']['问题模式'][0]['名称'] == '解对数不等式时遗漏定义域'
    assert '上一课时学情' in gen['context_note']


def test_grade_round_keeps_other_results_when_one_submission_errors(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    normal = judge_points(RULES)

    def grade(role, payload):
        if '触发错误' in payload['学生作答']:
            raise RuntimeError('boom')
        return normal(role, payload)

    ws.llm.handlers['grade'] = grade
    ws.import_submissions(pid, '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,触发错误')
    out = ws.grade_round(pid)
    assert out['graded'] == 1
    [failed] = out['failed']
    assert failed['learner'] == '乙' and 'RuntimeError' in failed['error']
    assert [g['content']['learner_alias'] for g in ws.gradings(pid)] == ['甲']

    ws.llm.handlers['grade'] = normal  # a retry grades only the submission that failed
    assert ws.grade_round(pid)['graded'] == 1
    assert {g['content']['learner_alias'] for g in ws.gradings(pid)} == {'甲', '乙'}


def _photo(color='white'):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(buf, format='PNG')
    return buf.getvalue()


def _transcribe(role, payload):
    assert payload['_images'] == 1 and [q['题号'] for q in payload['题目']] == [1]
    assert all('参考答案' not in q for q in payload['题目'])  # the reader never sees the key
    return {
        'answers': [
            {'question_no': 1, 'text': '定义域 x>1，又 x-1<8', 'uncertain': False, 'note': ''},
            {'question_no': 1, 'text': '所以 1<x<[看不清]', 'uncertain': True, 'note': '最后一个数字被涂改'},
            {'question_no': 7, 'text': '多出来的一题', 'uncertain': False, 'note': ''},
        ],
        'note': '',
    }


HANDLERS['transcribe'] = _transcribe


def test_photo_import_transcribes_then_imports_checked_text(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    draft = ws.transcribe_photos(pid, '甲', [_photo()])
    [row] = draft['answers']
    assert row['text'] == '定义域 x>1，又 x-1<8\n所以 1<x<[看不清]' and row['uncertain']
    assert len(draft['dropped']) == 1 and '第 7 题' in draft['dropped'][0]
    assert draft['context_note'] == '已参考：课时背景、题干 1 份、作答照片 1 份'
    [name] = draft['photos']
    assert ws.photo_path(name).exists()
    with ws.store.read() as c:
        g = c.execute('SELECT request FROM generations WHERE id=?', (draft['generation_id'],)).fetchone()
    assert 'base64' not in g['request']  # photos live on disk, not in the generation log
    assert ws.submissions(pid) == []  # nothing is imported before the teacher checks the text

    with pytest.raises(ValueError, match='看不清'):
        ws.import_photo_answers(pid, '甲', draft['answers'], photos=draft['photos'])
    fixed = [{**row, 'text': '定义域 x>1，又 x-1<8\n所以 1<x<9'}]
    with pytest.raises(Conflict):
        ws.import_photo_answers(pid, '丁', fixed, photos=draft['photos'])
    out = ws.import_photo_answers(pid, '甲', fixed, photos=draft['photos'], generation_id=draft['generation_id'])
    assert out == {'created': 1, 'problems': []}
    [sub] = ws.submissions(pid)
    assert sub['content']['source'] == 'photo' and sub['content']['photos'] == [name]
    assert ws.grade_round(pid)['graded'] == 1


def test_analysis_counts_learners_by_evidence_and_reports_objectives(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    q2 = ws.create_question(
        pid,
        {
            'type': 'calculation',
            'stem': '解不等式 log2(x-1) < 3（变式）',
            'reference': '1<x<9',
            'objective_ids': ['O1'],
        },
        {
            'points': [
                {'id': 'P1', 'description': '写出定义域 x>1', 'score': 2},
                {'id': 'P2', 'description': '结论 1<x<9', 'score': 2},
            ]
        },
    )
    ws.confirm(q2['id'])

    def analyze_q1(role, payload):
        group = next(g for g in payload['错误汇总'] if g['错误标签'].startswith('条件遗漏'))
        ev = next(e for e in group['证据'] if e['题号'] == 1)
        assert payload['目标达成'][0]['题号'] == [1, 2]
        return {
            'summary': '有人遗漏定义域。',
            'patterns': [
                {
                    'name': '第 1 题漏定义域',
                    'description': 'x',
                    'tags': [group['错误标签']],
                    'evidence': [{'eid': ev['eid'], 'quote': ev['学生原文'][:6]}],
                    'existing_pattern': '',
                }
            ],
            'insufficient': [],
        }

    ws.llm.handlers['analyze'] = analyze_q1
    ws.import_submissions(
        pid,
        '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9\n甲,2,x>1 所以 1<x<9\n乙,2,x<9\n丙,2,1<x<9',
    )
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    # Some roster/question entries are absent: analyze the received portion explicitly.
    out = ws.analyze(pid, partial=True)
    [pattern] = out['patterns']
    # 丙 lost the same tag on question 2 only: no longer counted for a question-1 pattern
    assert pattern['content']['affected_learners'] == ['乙']
    [obj] = out['analysis']['content']['objective_stats']
    assert obj['id'] == 'O1' and obj['题号'] == [1, 2]
    assert obj['人数'] == 3 and obj['得分率'] == 0.5 and obj['全部得分人数'] == 1


def test_comparison_counts_only_points_written_for_the_pattern(ws):
    p, _q = setup_round_one(ws)
    pid = p['id']
    ws.import_submissions(pid, '甲,1,定义域 x>1，又 x-1<8，所以 1<x<9\n乙,1,由单调性 x-1<8，所以 x<9')
    ws.grade_round(pid)
    ws.confirm_gradings(pid)
    # Some roster/question entries are absent: analyze the received portion explicitly.
    [pattern] = ws.analyze(pid, partial=True)['patterns']
    assert pattern['content']['affected_learners'] == ['乙']
    ws.confirm(pattern['id'])
    [pq] = ws.practice(pid)['questions']  # round 2, question 1: written for the pattern
    ws.confirm(pq['id'])
    other = ws.create_question(  # round 2, question 2: same skill, not written for the pattern
        pid,
        {'type': 'calculation', 'stem': '解不等式 log2(x-1) < 3（另一题）', 'reference': '1<x<9'},
        {
            'points': [
                {'id': 'P1', 'description': '写出定义域 x>1', 'score': 2},
                {'id': 'P2', 'description': '结论 1<x<9', 'score': 2},
            ]
        },
        round_=2,
    )
    ws.confirm(other['id'])
    ws.import_submissions(pid, '乙,1,x>1 所以 1<x<9\n乙,2,x<9\n丙,2,x<9\n甲,1,由单调性得 x<9', round_=2)
    ws.grade_round(pid, round_=2)
    ws.confirm_gradings(pid, round_=2)
    row = ws.compare(pid, 1, 2)['content']['patterns'][0]
    assert row['retested'] == ['乙', '甲'] or set(row['retested']) == {'甲', '乙'}
    assert row['improved'] == ['乙'] and row['persisting'] == []  # 乙 fixed it where it was retested
    assert row['new'] == ['甲']  # 丙 only lost the tag on a question not written for the pattern
    assert set(row['same_tag_elsewhere']) == {'乙', '丙'}
    assert row['not_retested'] == [] and row['enough_evidence']
