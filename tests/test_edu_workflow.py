"""Full teacher loop with a scripted model: objectives -> questions -> grading -> analysis -> practice -> comparison."""

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
    'draft_objectives': lambda r, p: {
        'objectives': [{'text': '能在解对数不等式时检查定义域', 'success_criterion': '写出真数大于零'}]
    },
    'generate_questions': lambda r, p: {'questions': [question('解不等式 log2(x-1) < 3')]},
    'practice': lambda r, p: {'questions': [question('解不等式 log3(x-2) < 2（巩固）', target=0)]},
    'self_check_solve': lambda r, p: {'answer': '定义域 x>1；单调递增得 x-1<8；所以 1<x<9', 'final_answer': '1<x<9'},
    'grade': judge_points(RULES),
    'review': judge_points(RULES),
    'analyze': analyze,
}


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    llm = FakeLLM(dict(HANDLERS))  # tests mutate handlers; never share the module-level dict
    return Workspace(Store(settings.db), llm, settings, rng=random.Random(1))


def setup_round_one(ws):
    p = ws.create_project('高一数学｜对数函数｜第2课时', '数学', grade='高一')
    ws.add_material(p['id'], '对数函数 y=log_a x 的定义域是 (0,+∞)。')
    objs = ws.draft_objectives(p['id'])
    assert objs['context_note'] == '已参考：项目背景、材料 1 份'
    ws.confirm(objs['objectives'][0]['id'])
    gen = ws.generate_questions(p['id'], types=['calculation'], count=1, ab=False)
    [q] = gen['candidates'][0]['questions']
    assert q['content']['checks']['solver'] == 'agree' and not q['content']['checks']['answer_suspect']
    assert '教学目标第 1 版（已确认）' in gen['context_note']
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
    p = ws.create_project('物理｜匀速直线运动', 'physics')
    ws.add_material(p['id'], '速度等于路程除以时间。')
    ws.confirm(ws.draft_objectives(p['id'])['objectives'][0]['id'])
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


def test_questions_need_confirmed_objectives(ws):
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
