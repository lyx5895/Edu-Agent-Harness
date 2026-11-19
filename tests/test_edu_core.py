"""edu_core building blocks: artifacts and staleness, context whitelist, tools, grading pipeline."""

import random

import pytest
from edu_fake import FakeLLM, judge_points, point

from edu_core import artifacts as A
from edu_core import tools
from edu_core.context import Context, ContextViolation, describe
from edu_core.grading import grade
from edu_core.llm import Gateway, ModelUnavailable, parse_json, strict_schema
from edu_core.storage import Store
from edu_core.subjects import SUBJECTS, error_tags, pack
from edu_core.workflow import QuestionSet

# ---------- artifacts ----------


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'edu.sqlite')


def test_versions_are_immutable_and_confirm_supersedes(store):
    with store.tx() as c:
        v1 = A.create(c, 'p', 'objective', {'text': 'a'}, origin='generated')
        A.confirm(c, v1['id'])
        v2 = A.edit(c, v1['id'], {'text': 'b'})
        assert v2['version'] == 2 and v2['lineage_id'] == v1['lineage_id'] and v2['status'] == 'draft'
        assert A.get(c, v1['id'])['status'] == 'confirmed'  # the draft edit does not replace the adopted version yet
        A.confirm(c, v2['id'])
        assert A.get(c, v1['id'])['status'] == 'superseded'
        assert A.get(c, v1['id'])['content'] == {'text': 'a'}
        with pytest.raises(A.Conflict):
            A.edit(c, v1['id'], {'text': 'c'})


def test_substantive_change_marks_downstream_transitively(store):
    with store.tx() as c:
        obj = A.create(c, 'p', 'objective', {'text': 'a'}, origin='teacher', status='confirmed')
        q = A.create(
            c, 'p', 'question', {'stem': 'q'}, origin='generated', refs=[(obj['id'], 'objective')], status='confirmed'
        )
        g = A.create(c, 'p', 'grading', {'total': 1}, origin='generated', refs=[(q['id'], 'question')])
        an = A.create(c, 'p', 'analysis', {'x': 1}, origin='generated', refs=[(g['id'], 'grading')])
        q2 = A.edit(c, q['id'], {'stem': 'q (typo fixed)'})
        A.confirm(c, q2['id'], change_kind='cosmetic')
        assert A.stale(c, 'p') == []
        q3 = A.edit(c, q2['id'], {'stem': 'different question'})
        A.confirm(c, q3['id'], change_kind='substantive')
        stale_ids = {m['artifact_id'] for m in A.stale(c, 'p')}
        assert stale_ids == {g['id'], an['id']}
        assert obj['id'] not in stale_ids  # upstream is untouched


def test_current_shows_latest_draft_with_confirmed_version(store):
    with store.tx() as c:
        v1 = A.create(c, 'p', 'objective', {'text': 'a'}, origin='teacher', status='confirmed')
        A.edit(c, v1['id'], {'text': 'b'})
        [cur] = A.current(c, 'p', 'objective')
        assert cur['version'] == 2 and cur['status'] == 'draft' and cur['confirmed_version'] == 1


# ---------- context whitelist ----------


def test_grading_context_cannot_read_history_or_unconfirmed(store):
    with store.tx() as c:
        q = A.create(c, 'p', 'question', {'stem': 'q'}, origin='teacher', status='confirmed')
        draft = A.create(c, 'p', 'rubric', {'points': []}, origin='teacher')
        other = A.create(c, 'p', 'grading', {'total': 1}, origin='generated', status='confirmed')
    ctx = Context('grade')
    ctx.add_artifact(q)
    with pytest.raises(ContextViolation):
        ctx.add_artifact(other)  # other students' gradings are not visible to the grader
    with pytest.raises(ContextViolation):
        ctx.add_artifact(draft)  # only confirmed versions flow downstream
    assert describe(ctx.manifest) == '已参考：题目第 1 版（已确认）'
    with pytest.raises(ContextViolation):
        Context('self_check_solve').add_artifact(q)  # the solver only sees the stem


# ---------- subject packs and tools ----------


def test_ten_subject_packs():
    assert {p['name'] for p in SUBJECTS.values()} == {
        '语文',
        '数学',
        '英语',
        '物理',
        '化学',
        '生物',
        '思想政治',
        '历史',
        '地理',
        '人工智能',
    }
    assert pack('人工智能')['id'] == 'ai' and 'writing' not in pack('ai')['types']
    assert '其他' in error_tags('math')
    with pytest.raises(ValueError):
        pack('美术')


def test_tools():
    assert tools.match_choice('B D', 'BD') is True and tools.match_choice('选A', 'B') is False
    assert tools.match_true_false('×', '错误') is True
    opts = ['A 和 B', 'A 和 C', 'B 和 D']
    assert tools.choice_from_option_text('A 和 C', opts) == {'B'}  # option text, not letters A and C
    assert tools.choice_from_option_text('B', opts) is None
    assert tools.check_numeric('所以平均速度为 54 km/h。', '15 m/s') == (True, '所以平均速度为 54 km/h')
    assert tools.check_numeric('答：x=3', '2')[0] is False
    assert tools.check_numeric('先算出 2。再化简。', '2')[0] is None  # final sentence has no number
    assert tools.algebra_equal('(a+b)^2', 'a^2+2ab+b^2') is True
    assert tools.algebra_equal('__import__("os")', 'x') is None  # never eval'd
    assert tools.chem_balanced('2KMnO4 =加热= K2MnO4 + MnO2 + O2↑') is True
    assert tools.chem_balanced('Fe + O2 = Fe3O4') is False
    # The tool decides only where it can (design doc 7.5).
    assert tools.check_numeric('由 5 和 3 得 8', '5')[0] is None  # 5 is not the stated result
    assert tools.check_numeric('所以结果为 5', '5')[0] is True
    want = '2H2 + O2 =点燃= 2H2O'
    assert tools.chem_equation_check('2H₂ + O₂ = 2H₂O', want) is True
    assert tools.chem_equation_check('H2 + O2 = H2O', want) is False  # unbalanced
    assert tools.chem_equation_check('H2 + O2 = H2O2', want) is None  # balanced, but another product
    assert tools.chem_equation_check('2H2 + O2 = 2H2O') is None  # nothing to compare the substances with


# ---------- grading ----------

Q = {'type': 'calculation', 'stem': '解不等式 log2(x-1) < 3', 'reference': '1<x<9'}
RUBRIC = {
    'points': [
        {'id': 'P1', 'description': '写出定义域 x>1', 'score': 2},
        {'id': 'P2', 'description': '结论 1<x<9', 'score': 2},
    ]
}
RULES = {'P1': ('x>1', '条件遗漏（定义域、取值范围等）'), 'P2': ('1<x<9', '结论缺失')}


def run(handlers, answer, **kw):
    llm = FakeLLM(handlers)
    result, _metas = grade(llm, subject='math', question=Q, rubric=RUBRIC, answer=answer, **kw)
    return result, llm


def test_all_earned_needs_no_review():
    result, llm = run({'grade': judge_points(RULES)}, '定义域 x>1，又 x-1<8，所以 1<x<9')
    assert result['total'] == 4 and not result['needs_teacher']
    assert llm.tasks() == ['grade']


def test_deduction_confirmed_by_reviewer_is_kept():
    result, llm = run({'grade': judge_points(RULES), 'review': judge_points(RULES)}, '由单调性 x-1<8，所以 x<9')
    assert [p['status'] for p in result['points']] == ['missed', 'missed']
    assert result['points'][0]['error_tag'] == '条件遗漏（定义域、取值范围等）'
    assert result['points'][0]['review']['agreed'] and not result['needs_teacher']
    review_call = llm.calls[1]
    assert review_call['role'] == 'reviewer' and '上次' not in review_call['user']
    assert 'score' not in str(review_call['user'].get('评分要点'))  # blind: no primary result is sent


def test_reviewer_disagreement_goes_to_teacher():
    lenient = {'P1': ('x', ''), 'P2': ('x', '')}
    result, _ = run({'grade': judge_points(RULES), 'review': judge_points(lenient)}, '由单调性 x-1<8，所以 x<9')
    assert all(p['status'] == 'undetermined' for p in result['points'])
    assert 'review_disagreement' in result['risks'] and result['needs_teacher']
    assert result['points'][0]['suggested']['status'] == 'missed'


def test_ungrounded_quote_is_retried_then_undetermined():
    calls = []

    def bad(role, payload):
        calls.append(payload)
        return {'points': [point('P1', 2, 'earned', '学生没写过这句'), point('P2', 2, 'earned', '1<x<9')]}

    result, _ = run({'grade': bad}, '定义域 x>1，又 x-1<8，所以 1<x<9')
    assert len(calls) == 2 and '上次的问题' in calls[1]
    assert result['points'][0]['status'] == 'undetermined' and result['points'][1]['status'] == 'earned'
    assert result['total'] == 2 and 'ungrounded_quote' in result['risks']


def test_low_confidence_is_undetermined():
    result, _ = run({'grade': judge_points(RULES, confidence='medium')}, '定义域 x>1，所以 1<x<9')
    assert result['undetermined'] == 2 and result['needs_teacher']


def test_keyed_and_empty_answers_skip_the_model():
    llm = FakeLLM({})
    mcq = {'type': 'single_choice', 'stem': '?', 'answer_key': 'B', 'options': ['1', '2']}
    one = {'points': [{'id': 'P1', 'description': '选对', 'score': 3}]}
    r, _ = grade(llm, subject='ai', question=mcq, rubric=one, answer='b')
    assert r['total'] == 3 and r['points'][0]['source'] == 'key'
    r, _ = grade(llm, subject='ai', question=mcq, rubric=one, answer='  ')
    assert r['total'] == 0 and r['points'][0]['error_tag'] == '未作答'
    assert llm.calls == []


def test_numeric_check_point_decided_by_tool():
    rubric = {
        'points': [{'id': 'P1', 'description': '结果', 'score': 2, 'check': {'kind': 'numeric', 'expected': '15 km/h'}}]
    }
    llm = FakeLLM({})
    r, _ = grade(
        llm, subject='physics', question={'type': 'calculation', 'stem': 's'}, rubric=rubric, answer='v=45/3\n=15 km/h'
    )
    assert r['points'][0]['source'] == 'tool' and r['total'] == 2


def test_balanced_equation_with_another_product_goes_to_the_model():
    rubric = {
        'points': [
            {
                'id': 'P1',
                'description': '方程式',
                'score': 2,
                'check': {'kind': 'chem_equation', 'expected': '2H2+O2=点燃=2H2O'},
            }
        ]
    }
    q = {'type': 'short_answer', 'stem': '写出氢气燃烧的化学方程式'}
    llm = FakeLLM(
        {
            'grade': lambda r, p: {'points': [point('P1', 0, 'missed', 'H2+O2=H2O2', tag='化学方程式错误')]},
            'review': lambda r, p: {'points': [point('P1', 0, 'missed', 'H2+O2=H2O2', tag='化学方程式错误')]},
        }
    )
    r, _ = grade(llm, subject='chemistry', question=q, rubric=rubric, answer='H2+O2=H2O2')
    assert r['points'][0]['source'] != 'tool' and r['total'] == 0  # balanced, but not water: not the tool's call
    r, _ = grade(FakeLLM({}), subject='chemistry', question=q, rubric=rubric, answer='2H2+O2=2H2O')
    assert r['points'][0]['source'] == 'tool' and r['total'] == 2
    r, _ = grade(FakeLLM({}), subject='chemistry', question=q, rubric=rubric, answer='H2+O2=H2O')
    assert r['points'][0]['source'] == 'tool' and r['points'][0]['error_tag'] == '配平错误'


def test_writing_is_suggested_only_and_not_reviewed():
    rubric = {'points': [{'id': 'P1', 'description': '内容与立意', 'score': 20}]}

    def h(role, payload):
        return {'points': [point('P1', 14, 'partial', '我的家乡', tag='语言表达问题')]}

    llm = FakeLLM({'grade': h})
    r, _ = grade(
        llm, subject='chinese', question={'type': 'writing', 'stem': '写家乡'}, rubric=rubric, answer='我的家乡在江南。'
    )
    assert r['suggested_only'] and r['needs_teacher'] and 'writing_needs_confirmation' in r['risks']
    assert llm.tasks() == ['grade']


def test_audit_samples_fully_earned_answers():
    lenient = judge_points(RULES)
    strict = judge_points({'P1': ('不存在', '其他'), 'P2': ('1<x<9', '')})
    result, llm = run(
        {'grade': lenient, 'audit': strict}, '定义域 x>1，所以 1<x<9', audit_rate=1.0, rng=random.Random(0)
    )
    assert 'audit_disagreement' in result['risks'] and result['total'] == 4
    assert llm.tasks() == ['grade', 'audit']


# ---------- gateway parsing ----------


def test_parse_json_and_strict_schema():
    assert parse_json('{"a": 1}\n\n   \r\n  ') == {'a': 1}  # trailing whitespace runs seen from DeepSeek
    assert parse_json('```json\n{"a": 2}\n```') == {'a': 2}
    schema = strict_schema(QuestionSet)
    item = schema['properties']['questions']['items']
    assert item['additionalProperties'] is False and set(item['required']) == set(item['properties'])
    point = item['properties']['points']['items']
    assert 'description' in point['properties'] and 'description' in point['required']  # a property, not metadata


def test_gateway_repairs_once_then_fails():
    import httpx

    replies = iter(['[1, 2]', '{"questions": []}'])

    def handler(request):
        return httpx.Response(
            200,
            json={
                'model': 'm-1',
                'choices': [{'message': {'content': next(replies)}, 'finish_reason': 'stop'}],
                'usage': {'total_tokens': 5},
            },
        )

    gw = Gateway('http://x/v1', 'k', {'generator': 'm'}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    out, meta = gw.complete('generator', 't', 's', 'u', QuestionSet)
    assert (
        out.questions == []
        and meta['repaired']
        and meta['model_returned'] == 'm-1'
        and meta['usage']['total_tokens'] == 10
    )

    def always_bad(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]})

    gw = Gateway('http://x/v1', 'k', {'generator': 'm'}, client=httpx.Client(transport=httpx.MockTransport(always_bad)))
    with pytest.raises(ModelUnavailable):
        gw.complete('generator', 't', 's', 'u', QuestionSet)


def test_math_curriculum_items_are_verified_and_searchable():
    from edu_core import curriculum as C

    data = C.load('math')
    ids = [it['id'] for it in data['items']]
    assert len(ids) == len(set(ids)) and sum(it['kind'] == 'content' for it in data['items']) == 154
    assert data['source_url'].startswith('http://www.moe.gov.cn/') and len(data['source_sha256']) == 64
    assert data['by_id']['math.B2.2.3.3']['text'] == '知道对数函数y=logax与指数函数y=ax互为反函数(a>0,且a≠1)。'
    top = C.search('math', '对数函数 对数的概念与运算')[0]
    assert top['id'] == 'math.B2.2.3.1'
    assert C.search('math', '') == [] and C.load('art') is None
    assert (C.level('了解对数函数的概念'), C.level('理解对数的概念'), C.level('熟练掌握')) == (1, 2, 3)
    ctx = C.context('math', ['math.B2.2.3.2', 'math.X9'], '对数函数')
    assert [it['id'] for it in ctx['content']] == ['math.B2.2.3.2']
    assert all(it['id'].startswith('math.B2.') for it in ctx['hints'] + ctx['requirements'])


@pytest.mark.parametrize(
    'subject,query,expected',
    [
        ('chinese', '整本书阅读', 'chinese.G1.'),
        ('english', '定语从句', 'english.B6.'),
        ('politics', '矛盾分析法', 'politics.B4.1.4'),
        ('history', '辛亥革命', 'history.B1.1.8'),
        ('geography', '大气受热过程 热力环流', 'geography.B1.1.6'),
        ('physics', '匀变速直线运动', 'physics.B1.1.1.3'),
        ('chemistry', '氧化还原反应', 'chemistry.B2.2.2'),
        ('biology', '种群数量变化', 'biology.X2.'),
        ('ai', '人工智能 机器学习', 'ai.X4.'),
    ],
)
def test_every_subject_has_verified_curriculum_items(subject, query, expected):
    from edu_core import curriculum as C

    data = C.load(subject)
    assert data['source_url'].startswith('http://www.moe.gov.cn/') and len(data['source_sha256']) == 64
    content = [it for it in data['items'] if it['kind'] == 'content']
    assert len(content) >= 40 and all(it['text'] and it['short'] for it in content)
    assert len({it['id'] for it in data['items']}) == len(data['items'])
    assert C.search(subject, query)[0]['id'].startswith(expected)


def _old_database(path):
    """A database as an earlier build left it: no revision marker and none of the added project columns."""
    import sqlite3

    from edu_core.storage import SCHEMA

    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO projects(id,title,subject,created_at) VALUES('proj_old','旧项目','数学',0)")
    c.commit()
    c.close()


def test_store_migrates_an_older_database(tmp_path):
    import sqlite3

    from edu_core.storage import SCHEMA_VERSION

    path = tmp_path / 'old.sqlite'
    _old_database(path)
    for _ in range(2):  # re-opening an up-to-date database is a no-op
        c = Store(path).connect()
        try:
            assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
            row = c.execute("SELECT knowledge_module, curriculum FROM projects WHERE id='proj_old'").fetchone()
            assert tuple(row) == ('', '')
        finally:
            c.close()
    assert sqlite3.connect(path).execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION


def test_store_leaves_a_newer_database_alone(tmp_path):
    path = tmp_path / 'newer.sqlite'
    _old_database(path)
    c = Store(path).connect()
    c.execute('PRAGMA user_version=99')
    c.close()
    c = Store(path).connect()  # must not re-run migrations on a database from a newer build
    assert c.execute('PRAGMA user_version').fetchone()[0] == 99
    c.close()


def test_store_migrates_a_database_that_already_has_the_columns(tmp_path):
    """Databases from the column-by-column startup have the columns but user_version 0."""
    import sqlite3

    from edu_core.storage import SCHEMA_VERSION

    path = tmp_path / 'patched.sqlite'
    _old_database(path)
    c = sqlite3.connect(path)
    for column in ('knowledge_module', 'lesson', 'learner_profile', 'prev_project_id', 'carryover', 'lesson_scope', 'curriculum'):
        c.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    c.execute("UPDATE projects SET lesson='第 2 课时'")
    c.commit()
    c.close()
    c = Store(path).connect()
    try:
        assert c.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert c.execute("SELECT lesson FROM projects WHERE id='proj_old'").fetchone()[0] == '第 2 课时'
    finally:
        c.close()


def test_photo_prepare_rejects_non_images_and_turns_upright(tmp_path):
    import io

    from PIL import Image

    from edu_core import images

    with pytest.raises(ValueError):
        images.prepare(b'not an image')
    im = Image.new('RGB', (40, 20), 'white')
    exif = im.getexif()
    exif[0x0112] = 6  # rotated 90 degrees, as phones store it
    buf = io.BytesIO()
    im.save(buf, format='JPEG', exif=exif)
    name, raw = images.prepare(buf.getvalue())
    assert Image.open(io.BytesIO(raw)).size == (20, 40)
    assert images.store(name, raw, tmp_path).read_bytes() == raw
    with pytest.raises(ValueError):
        images.path('../edu.sqlite', tmp_path)


def test_redact_images_keeps_text_and_leaves_the_request_alone():
    from edu_core.llm import redact_images

    msgs = [
        {
            'role': 'user',
            'content': [{'type': 'text', 'text': 'hi'}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAAA'}}],
        }
    ]
    out = redact_images(msgs)
    assert out[0]['content'][0]['text'] == 'hi' and 'base64' not in str(out)
    assert 'base64' in str(msgs)


def test_strict_schema_uses_inclusive_bounds():
    """Gemini rejects exclusiveMinimum/exclusiveMaximum; pydantic still enforces the strict bound."""
    schema = strict_schema(QuestionSet)
    text = str(schema)
    assert 'exclusiveMinimum' not in text and 'exclusiveMaximum' not in text
    assert "'minimum': 0" in text
