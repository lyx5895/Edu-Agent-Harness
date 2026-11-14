import json

import pytest

from harness.resources.knowledge import Knowledge, import_kgraph, tag
from harness.llm.model import ModelUnavailable
from harness.core.open_engine import OpenEngine
from test_open_tasks import FakeOpenModel
from test_teaching_v04 import assignment as base

NODES = [
    {'id': 'math_7a_rjb', 'label': 'Book', 'name': '七年级上册'},
    {'id': 'math_7a_rjb_ch3', 'label': 'Chapter', 'name': '第三章 一元一次方程'},
    {'id': 'math_7a_rjb_ch3_s2', 'label': 'Section', 'name': '3.2 解一元一次方程'},
    {'id': 'math_7a_rjb_cpt1', 'label': 'Concept', 'name': '等式的性质'},
    {'id': 'math_7a_rjb_cpt2', 'label': 'Concept', 'name': '一元一次方程'},
    {'id': 'math_7a_rjb_skl1', 'label': 'Skill', 'name': '移项与合并同类项解方程'},
    {'id': 'math_7a_rjb_exe1', 'label': 'Exercise', 'name': '解方程 2x+3=7'},
    {'id': 'math_bx1_rjb', 'label': 'Book', 'name': '必修一'},
    {'id': 'math_bx1_rjb_ch1', 'label': 'Chapter', 'name': '第一章 集合'},
    {'id': 'math_bx1_rjb_ch1_s1', 'label': 'Section', 'name': '1.1 集合的概念'},
    {'id': 'math_bx1_rjb_cpt1', 'label': 'Concept', 'name': '集合'},
    {'id': 'physics_8a_rjb', 'label': 'Book', 'name': '八年级上册'},
    {'id': 'physics_8a_rjb_cpt1', 'label': 'Concept', 'name': '方程式化的速度公式'},
]
EDGES = [
    {'source': 'math_7a_rjb_ch3', 'target': 'math_7a_rjb', 'type': 'is_part_of'},
    {'source': 'math_7a_rjb_ch3_s2', 'target': 'math_7a_rjb_ch3', 'type': 'is_part_of'},
    {'source': 'math_bx1_rjb_ch1', 'target': 'math_bx1_rjb', 'type': 'is_part_of'},
    {'source': 'math_bx1_rjb_ch1_s1', 'target': 'math_bx1_rjb_ch1', 'type': 'is_part_of'},
    *[{'source': f'math_7a_rjb_{n}', 'target': 'math_7a_rjb_ch3_s2', 'type': 'appears_in'} for n in ('cpt1', 'cpt2', 'skl1', 'exe1')],
    {'source': 'math_bx1_rjb_cpt1', 'target': 'math_bx1_rjb_ch1_s1', 'type': 'appears_in'},
    {'source': 'math_7a_rjb_cpt1', 'target': 'math_7a_rjb_skl1', 'type': 'prerequisites_for'},
    {'source': 'math_7a_rjb_cpt2', 'target': 'math_7a_rjb_skl1', 'type': 'prerequisites_for'},
    {'source': 'math_7a_rjb_exe1', 'target': 'math_7a_rjb_skl1', 'type': 'tests_skill'},
]


def assignment(concept_id):
    return {**base(), 'concept_id': concept_id}


@pytest.fixture
def kb(tmp_path):
    (tmp_path / 'nodes.json').write_text(json.dumps(NODES, ensure_ascii=False), encoding='utf-8')
    (tmp_path / 'edges.json').write_text(json.dumps(EDGES, ensure_ascii=False), encoding='utf-8')
    import_kgraph(tmp_path / 'nodes.json', tmp_path / 'edges.json', tmp_path / 'kp.sqlite')
    return Knowledge(tmp_path / 'kp.sqlite')


class Tagger:
    def __init__(self, guesses=(), chosen=(), fail=False):
        self.guesses, self.chosen, self.fail, self.seen = list(guesses), list(chosen), fail, []

    def guess_knowledge(self, question, subject):
        if self.fail: raise ModelUnavailable('down')
        return self.guesses

    def choose_knowledge(self, question, subject, candidates):
        self.seen = [c['id'] for c in candidates]
        if self.fail: raise ModelUnavailable('down')
        return self.chosen


def test_import_derives_location_stage_and_prerequisites(kb):
    node = kb.get('math_7a_rjb_skl1')
    assert (node['subject'], node['stage'], node['book_name'], node['chapter_name'], node['section_name']) == \
        ('math', '初中', '七年级上册', '第三章 一元一次方程', '3.2 解一元一次方程')
    assert kb.get('math_bx1_rjb_cpt1')['stage'] == '高中'
    assert [p['name'] for p in kb.prerequisites('math_7a_rjb_skl1')] == ['等式的性质', '一元一次方程']
    assert kb.meta()['license'] == 'CC BY-NC-SA 4.0'


def test_search_only_returns_taggable_nodes_filtered_by_subject_and_stage(kb):
    ids = [n['id'] for n in kb.search('解一元一次方程', '数学')]
    assert 'math_7a_rjb_exe1' not in ids and 'math_7a_rjb_cpt2' in ids
    assert all(i.startswith('math_') for i in ids)
    assert kb.search('方程', 'physics')[0]['id'] == 'physics_8a_rjb_cpt1'
    assert kb.search('集合', '数学', ['初中']) == []


def test_tag_keeps_only_library_ids_and_falls_back_to_bm25(kb):
    model = Tagger(guesses=['等式的性质'], chosen=['math_7a_rjb_cpt1', 'invented_id', 'math_7a_rjb_cpt1'])
    result = tag(model, kb, '解方程 2x+3=7，说明每一步依据', '数学')
    assert result['ids'] == ['math_7a_rjb_cpt1'] and result['source'] == 'model'
    assert model.seen[0] == 'math_7a_rjb_cpt1'
    result = tag(Tagger(chosen=['invented_id']), kb, '一元一次方程', '数学')
    assert result['source'] == 'bm25' and result['ids'][0] == 'math_7a_rjb_cpt2'
    assert tag(Tagger(fail=True), kb, '一元一次方程', '数学')['source'] == 'bm25'
    exact = tag(Tagger(guesses=['一元一次方程'], chosen=['math_7a_rjb_cpt1']), kb, '解方程 2x+3=7', '数学')
    assert exact['ids'] == ['math_7a_rjb_cpt2', 'math_7a_rjb_cpt1']


def test_engine_suggests_and_rejects_unknown_library_ids(tmp_path, kb):
    model = FakeOpenModel()
    model.guess_knowledge = lambda q, s: ['解方程']
    model.choose_knowledge = lambda q, s, c: ['math_7a_rjb_skl1']
    e = OpenEngine(tmp_path / 'db.sqlite', model)
    e.knowledge = kb
    out = e.suggest_knowledge('解方程 2x+3=7', '数学')
    assert out['items'][0]['id'] == 'math_7a_rjb_skl1'
    assert [p['name'] for p in out['items'][0]['prerequisites']] == ['等式的性质', '一元一次方程']
    with pytest.raises(ValueError, match='只覆盖'):
        e.suggest_knowledge('赏析这首诗', '语文')
    assert e.publish_assignment(assignment(concept_id='math_7a_rjb_skl1'))['assignment_id']
    with pytest.raises(ValueError, match='没有这些编号'):
        e.publish_assignment(assignment(concept_id='math_7a_rjb_cpt99'))
    assert e.publish_assignment(assignment(concept_id='自定义-方程'))['assignment_id']
