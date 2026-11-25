"""义务教育 (2022) standards: grade selection, and that a lower-secondary lesson never sees senior-high items."""

import random
import re

import pytest

from edu_core import curriculum as C
from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


@pytest.fixture
def ws(tmp_path):
    settings = load_settings(db=str(tmp_path / 'edu.sqlite'), audit_rate=0.0, ab_rate=0.0, concurrency=2)
    return Workspace(Store(settings.db), None, settings, rng=random.Random(1))


@pytest.mark.parametrize(
    'text,expected',
    [
        ('初二', (8, 8)),
        ('八年级', (8, 8)),
        ('五年级', (5, 5)),
        ('3年级', (3, 3)),
        ('高一', (10, 10)),
        ('初中', (7, 9)),
        ('小学', (1, 6)),
        ('', None),
        ('对数函数', None),
    ],
)
def test_grade_range_reads_the_usual_spellings(text, expected):
    assert C.grade_range(text) == expected


def test_source_picks_the_compulsory_set_for_grades_1_to_9():
    assert C.source('math', '初二') == 'math@8-8'
    assert C.source('math', '五年级', '高二数学') == 'math@5-5'  # the grade field decides before the title
    assert C.source('math', '', '初二数学') == 'math@8-8'
    assert C.source('math', '高一') == 'math'  # senior high, and the default
    assert C.source('math', '', '对数函数') == 'math'
    assert C.source('机器人', '初一') == '机器人@7-7'  # a subject without standards still gets a key; load() says no


@pytest.mark.parametrize('subject', ['math', 'chinese', 'english'])
def test_compulsory_items_carry_grades_and_their_source(subject):
    data = C.load(f'{subject}@7-9')
    assert (
        data['title']
        == f'义务教育{"数学" if subject == "math" else "语文" if subject == "chinese" else "英语"}课程标准（2022年版）'
    )
    assert data['source_url'].startswith('http://www.moe.gov.cn/') and len(data['source_sha256']) == 64
    assert 'MinerU' in data['text_source']  # the scans have no text layer: say so
    items = data['items']
    assert len({it['id'] for it in items}) == len(items)
    assert all(1 <= it['grades'][0] <= it['grades'][1] <= 9 for it in items)
    assert all(it['text'].strip() and it['label'] and it['page'] > 0 for it in items)


def test_a_lower_secondary_lesson_never_matches_senior_high_items():
    junior = C.load('math@8-8')
    assert all(it['id'].startswith('math.S4') for it in junior['items'])  # 第四学段 = 7–9 年级
    top = C.search('math@8-8', '一次函数 图象与性质')
    assert top and all(it['id'].startswith('math.S4A.3') for it in top[:3])
    ctx = C.context('math@8-8', [top[0]['id']], '一次函数')
    # the 学业要求 / 教学提示 of the same 学段·领域 come along, and nothing from 必修/选择性必修
    assert all(it['id'].startswith('math.S4') for it in ctx['requirements'] + ctx['hints'])
    assert C.load('math@8-8')['stage_name'] == '义务教育·8年级'


def test_primary_grades_see_their_own_stage_only():
    three = C.load('math@3-3')
    assert {it['stage'] for it in three['items']} == {'第二学段（3～4年级）'}
    five = C.load('math@5-5')
    assert {it['stage'] for it in five['items']} == {'第三学段（5～6年级）'}
    assert all(not it['id'].startswith('math.S4') for it in five['items'])


def test_english_plus_items_are_marked_optional_and_never_auto_picked():
    data = C.load('english@7-9')
    plus = [it for it in data['items'] if it.get('optional')]
    assert len(plus) >= 5
    from edu_core.workflow.projects import ProjectsMixin

    found = C.search('english@7-9', ' '.join(it['text'][:12] for it in plus[:2]), k=8)
    assert any(it.get('optional') for it in found)  # offered to the teacher …
    assert not any(it.get('optional') for it in ProjectsMixin._auto_pick(found))  # … but never picked for them


def test_grade_one_english_has_no_content_items():
    # 预备级 (1–2 年级) has 教学提示 only; the planner should be told there is no standard rather than get hints
    assert C.load('english@1-1') is None


def test_chinese_task_groups_are_tied_to_their_stage():
    data = C.load('chinese@4-4')
    content = [it for it in data['items'] if it['kind'] == 'content']
    assert content and all(it['stage'] == '第二学段（3～4年级）' for it in content)
    assert all(it['id'].startswith('chinese.S2') for it in content)
    hints = [it for it in data['items'] if it['kind'] == 'hint']
    assert hints and all(it['groups'][0].startswith('T') for it in hints)


def test_unknown_grades_fall_back_to_senior_high_and_missing_grades_are_refused():
    assert C.load('math@1-9') is not None  # spans both: the 义务教育 set still has items
    assert C.load('art@3-3') is None
    assert C.load('math@0-3') is None  # malformed key
    assert C.stage_name('math') == '普通高中' and C.stage_name('math@7-9') == '义务教育·7～9年级'


def test_compulsory_text_is_clean_of_markup():
    for subject in ('math', 'chinese', 'english'):
        data = C.load(f'{subject}@7-9')
        for it in data['items']:
            text = it['text']
            assert '$' not in text and '\\frac' not in text and not re.search(r'</?[a-z]', text), it['id']


def test_workspace_matches_the_standard_of_the_lesson_grade(ws):
    pid = ws.create_project('一年级英语', 'english', grade='一年级')['id']
    view = ws.curriculum(pid)
    assert view == {'available': False}  # nothing to match 预备级 against
    pid = ws.create_project('初二数学', 'math', grade='初二')['id']
    view = ws.curriculum(pid, q='一次函数')
    assert view['available'] and view['title'].startswith('义务教育数学')
    assert view['stage'] == '义务教育·8年级'
    assert all(it['id'].startswith('math.S4') for it in view['items'])
    assert 'MinerU' in view['text_source']
    # moving the lesson to 高一 drops picks that only exist in the 义务教育 set
    ws.update_project(pid, curriculum_ids=[view['items'][0]['id']])
    assert ws.curriculum(pid)['selected'] == [view['items'][0]['id']]
    ws.update_project(pid, grade='高一')
    after = ws.curriculum(pid, q='一次函数')
    assert after['stage'] == '普通高中' and after['chosen_by'] is None


@pytest.mark.parametrize(
    'subject,grade,query,expected',
    [
        ('physics', '初二', '密度 测量', 'physics.M'),
        ('physics', '初三', '欧姆定律', 'physics.M3.'),
        ('chemistry', '初三', '质量守恒定律', 'chemistry.M4.'),
        ('biology', '初一', '细胞 结构', 'biology.M1.'),
        ('biology', '八年级', '遗传 基因', 'biology.M6.'),
        ('history', '初二', '辛亥革命', 'history.M2.'),
        ('history', '初三', '工业革命', 'history.M5.'),
        ('geography', '初一', '地球自转 公转', 'geography.M2.'),
        ('politics', '初二', '宪法 公民权利', 'politics.S4.'),
        ('politics', '一年级', '入学 上学', 'politics.S1.'),
        ('science', '三年级', '空气 水', 'science.C1.S2.'),
        ('ai', '四年级', '数据 编码', 'ai.S2.'),
        ('ai', '八年级', '人工智能', 'ai.S4.'),
    ],
)
def test_other_subjects_match_their_lesson_in_the_compulsory_standard(subject, grade, query, expected):
    key = C.source(subject, grade)
    data = C.load(key)
    assert data['title'].startswith('义务教育') and 'MinerU' in data['text_source']
    assert len({it['id'] for it in data['items']}) == len(data['items'])
    assert C.search(key, query)[0]['id'].startswith(expected)


def test_junior_only_subjects_have_no_primary_standard():
    for subject in ('physics', 'chemistry', 'biology', 'history', 'geography'):
        assert C.load(f'{subject}@5-5') is None  # 小学没有这些课
        assert C.load(f'{subject}@7-7') is not None  # 初中各年级都可用：课标不规定主题放在哪个年级


def test_science_points_follow_their_grade_band():
    # 电路 is taught in 3–4 年级 and again in 7–9 年级; a 五年级 lesson does not get either
    assert C.search('science@4-4', '电路')[0]['id'].startswith('science.C3.S2.')
    assert C.search('science@5-5', '电路') == []


def test_politics_bullets_keep_dates_whole():
    data = C.load('politics@5-6')
    assert any('“12·4”国家宪法日' in it['text'] for it in data['items'])


def test_compulsory_subject_names_resolve_to_their_packs():
    from edu_core.subjects import pack

    assert pack('道德与法治')['id'] == 'politics'
    assert pack('信息科技')['id'] == 'ai'
    assert pack('科学')['id'] == 'science' and C.load('science') is None  # 科学: 义务教育 only


@pytest.mark.parametrize(
    'subject,grade,expected',
    [
        ('politics', '初二', '道德与法治'),
        ('politics', '三年级', '道德与法治'),
        ('politics', '高一', '思想政治'),
        ('politics', '', '思想政治'),  # no grade: senior high, as before
        ('ai', '五年级', '信息科技'),
        ('ai', '高二', '人工智能'),
        ('math', '初二', '数学'),
        ('美术', '初二', '美术'),  # a custom subject keeps the teacher's own name
    ],
)
def test_subject_is_named_for_its_stage(subject, grade, expected):
    from edu_core.subjects import subject_name

    assert subject_name(subject, grade) == expected


def test_pickers_list_both_names_and_accept_either():
    from edu_core.subjects import SUBJECTS, subject_aliases, subject_label

    assert subject_label(SUBJECTS['politics']) == '思想政治 / 道德与法治'
    assert subject_label(SUBJECTS['ai']) == '人工智能 / 信息科技'
    assert subject_label(SUBJECTS['math']) == '数学'
    assert '道德与法治' in subject_aliases(SUBJECTS['politics'])
    assert '信息科技' in subject_aliases(SUBJECTS['ai'])


def test_lesson_pages_and_prompts_use_the_stage_name(ws):
    junior = ws.create_project('初二·宪法', 'politics', grade='初二')
    senior = ws.create_project('高一·经济', 'politics', grade='高一')
    assert junior['subject_name'] == '道德与法治' and senior['subject_name'] == '思想政治'
    assert junior['subject'] == senior['subject'] == 'politics'  # one subject pack, one id; only the name differs
    with ws.store.read() as c:
        p = ws._project(c, junior['id'])
    assert p['pack']['name'] == '道德与法治'  # every prompt formats {subject} from this
    from edu_core.subjects import SUBJECTS

    assert SUBJECTS['politics']['name'] == '思想政治'  # the shared pack itself is not touched
    moved = ws.update_project(junior['id'], grade='高三')
    assert moved['subject_name'] == '思想政治'


def test_courses_are_named_for_their_stage(ws):
    crs = ws.create_course('八年级信息课', 'ai', grade='八年级')
    with ws.store.read() as c:
        assert ws._course(c, crs['id'])['subject_name'] == '信息科技'


def test_grading_prompt_uses_the_given_subject_name():
    from edu_core import grading

    body = grading._payload('politics', {'type': 'short_answer', 'stem': '题'}, [], '答', [], name='道德与法治')
    assert '"学科": "道德与法治"' in body
    assert '"学科": "思想政治"' in grading._payload('politics', {'type': 'short_answer'}, [], '答', [])
