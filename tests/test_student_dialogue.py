"""Replies to a follow-up question are dialogue; only attempts are graded."""

import pytest
from test_open_tasks import FakeOpenModel, act

from harness.core.open_engine import OpenEngine
from harness.errors import Conflict


def test_reply_is_dialogue_and_complete_answer_is_graded(tmp_path):
    engine = OpenEngine(tmp_path / 'dialogue.sqlite', FakeOpenModel())
    s = engine.create('学生', '解释月相变化')
    first = act(engine, s, 'attempt', '错误解释')
    assert first['status'] == 'incorrect'
    assert first['session']['pending_question']
    reply = act(engine, first['session'], 'reply', '我不确定')
    assert reply['evidence'] == 'dialogue_only'
    corrected = act(engine, reply['session'], 'attempt', '正确解释')
    assert corrected['status'] == 'correct'
    assert corrected['session']['completed']


def test_check_phase_rejects_help(tmp_path):
    engine = OpenEngine(tmp_path / 'check.sqlite', FakeOpenModel())
    s = engine.create('学生', '解释月相变化')
    done = act(engine, s, 'attempt', '正确解释')['session']
    check = act(engine, done, 'check')['session']
    assert check['task']['phase'] == 'check'
    for kind in ('hint', 'explain'):
        with pytest.raises(Conflict):
            act(engine, check, kind)


def test_view_reports_the_reply_actually_delivered(tmp_path):
    model = FakeOpenModel()
    model.bad_review = True  # every draft is rejected, so a server-owned reply is delivered instead
    engine = OpenEngine(tmp_path / 'fallback.sqlite', model)
    s = engine.create('学生', '解释月相变化')
    turn = act(engine, s, 'attempt', '错误解释')['session']['history'][-1]
    assert turn['action'] == 'locate'
    assert turn['delivered_action'] == 'locate_template'
    hint = act(engine, engine.session(s['session_id']), 'hint')['session']['history'][-1]
    assert hint['action'] == 'scaffold_concept' and hint['delivered_action'] == 'scaffold_concept_template'


class NumericModel(FakeOpenModel):
    """Writes explanations that stop before the answer, like the live tutor sometimes does."""

    def __init__(self):
        super().__init__()
        self.reviews = []

    def respond(self, task, action, text, context, assessment, revision=None):
        if action == 'explain':
            return '平均速度是路程除以时间。请计算 45 ÷ 3 等于多少？'
        return '速度表示每小时走多少路，几小时后总路程和每小时的路程有什么关系？'

    def review(self, task, candidate, action, student=None):
        self.reviews.append((action, student))
        return {'acceptable': True, 'reason': '检查结果'}


class RewritingReviewer(NumericModel):
    model_name = 'reviewer'

    def respond(self, task, action, text, context, assessment, revision=None):
        if action == 'explain':
            return '速度 = 路程 ÷ 时间，45 ÷ 3 = 15，所以平均速度是 15 千米/小时。接下来做一道新题检查自己。'
        return super().respond(task, action, text, context, assessment, revision)


def _speed(engine):
    return engine.publish_assignment(
        dict(class_id='C', course_id='数学', concept_id='速度', domain='数学', objective='求平均速度',
             question='小明 3 小时行了 45 千米，平均速度是多少？', rubric='路程除以时间，带单位', reference='45 ÷ 3 = 15 千米/小时')
    )['assignment_id']  # fmt: skip


def test_explanation_must_reach_the_answer(tmp_path):
    engine = OpenEngine(tmp_path / 'explain.sqlite', NumericModel())
    s = engine.create('学生', assignment_id=_speed(engine))
    act(engine, s, 'attempt', '45 × 3 = 135')
    turn = act(engine, engine.session(s['session_id']), 'explain')
    trace = engine.traces(s['session_id'])[-1]['output_validation']
    assert 'explain_incomplete' in trace['reasons']
    assert turn['session']['history'][-1]['delivered_action'] == 'clarify'  # the failed-explanation notice


def test_reviewer_rewrites_rejected_explanations_and_sees_the_learners_work(tmp_path):
    reviewer = RewritingReviewer()
    engine = OpenEngine(tmp_path / 'rewrite.sqlite', NumericModel(), reviewer=reviewer)
    s = engine.create('学生', assignment_id=_speed(engine))
    act(engine, s, 'attempt', '45 × 3 = 135')
    turn = act(engine, engine.session(s['session_id']), 'hint')['session']['history'][-1]
    assert turn['delivered_action'] == 'scaffold_concept'
    # With a reviewer configured, it alone reviews hints, and it sees the learner's latest attempt.
    assert ('scaffold_concept', '45 × 3 = 135') in reviewer.reviews
    assert all(action == 'locate' for action, _ in engine.model._model.reviews)
    turn = act(engine, engine.session(s['session_id']), 'explain')['session']['history'][-1]
    assert turn['delivered_action'] == 'explain' and '15' in turn['feedback']
    trace = engine.traces(s['session_id'])[-1]['output_validation']
    assert trace['first_draft']['reasons'] == ['explain_incomplete'] and trace['rewrite_model'] == 'reviewer'


def test_hint_that_computes_with_the_problems_numbers_is_rejected(tmp_path):
    from harness.core.evidence import computes_given_numbers

    task = {'question': '小明 3 小时行了 45 千米，平均速度是多少？'}
    assert computes_given_numbers(task, '请用 45 除以 3 算一算')
    assert computes_given_numbers(task, '45÷3=?')
    assert not computes_given_numbers(task, '速度 = 路程 ÷ 时间，找出题目里的路程和时间')
    assert not computes_given_numbers(task, '例子：20 ÷ 2 = 10')

    class Revealing(NumericModel):
        def respond(self, task, action, text, context, assessment, revision=None):
            return '速度是每小时走的路，请用 45 除以 3 算出结果。'

    engine = OpenEngine(tmp_path / 'reveal.sqlite', Revealing())
    s = engine.create('学生', assignment_id=_speed(engine))
    act(engine, s, 'attempt', '45 × 3 = 135')
    turn = act(engine, engine.session(s['session_id']), 'hint')['session']['history'][-1]
    assert turn['delivered_action'] == 'scaffold_concept_template'
    assert 'computes_for_student' in engine.traces(s['session_id'])[-1]['output_validation']['reasons']
