"""Evidence dependencies are recorded as edges and drive correction invalidation."""

import sqlite3

from test_open_tasks import FakeOpenModel, act

from harness.core.open_engine import OpenEngine


def _assignment(engine):
    return engine.publish_assignment(
        dict(
            class_id='C',
            course_id='科学',
            concept_id='月相',
            domain='科学',
            objective='解释月相',
            question='月相为何变化',
            rubric='解释原因',
            reference='参考结论',
        )
    )['assignment_id']


def _chain(engine):
    """Three sessions on one goal; each later one imports the earlier evidence as memory."""
    aid = _assignment(engine)
    sessions = []
    for _ in range(3):
        s = engine.create('A', assignment_id=aid)
        turn = act(engine, s, 'attempt', '错误解释')
        sessions.append((s['session_id'], turn['event_id']))
    return sessions


def test_context_imports_are_recorded_as_edges(tmp_path):
    engine = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    (first, _), (second, _), (third, _) = _chain(engine)
    with engine.connect() as c:
        edges = {tuple(r) for r in c.execute('SELECT consumer, source FROM evidence_dependencies')}
    assert (second, first) in edges and (third, first) in edges
    assert not any(consumer == first for consumer, _ in edges)


def test_existing_database_is_backfilled_from_traces(tmp_path):
    path = tmp_path / 'old.sqlite'
    engine = OpenEngine(path, FakeOpenModel())
    (first, event), (second, _), (third, _) = _chain(engine)
    # Simulate a database written before the table existed.
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE evidence_dependencies')
        c.execute('DELETE FROM harness_migrations')
    reopened = OpenEngine(path, FakeOpenModel())
    result = reopened.correct(first, event, 'unknown', '原题有歧义', 'fix')
    assert set(result['invalidated_sessions']) == {first, second, third}
    assert reopened.session(third)['needs_review']


def test_correction_only_reaches_dependent_sessions(tmp_path):
    engine = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    (first, event), *_ = _chain(engine)
    unrelated = engine.create('B', '解释四季成因')
    act(engine, unrelated, 'attempt', '错误解释')
    result = engine.correct(first, event, 'unknown', '原题有歧义', 'fix')
    assert unrelated['session_id'] not in result['invalidated_sessions']
    assert not engine.session(unrelated['session_id'])['needs_review']
