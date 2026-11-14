"""Transactions compute on a WAL read snapshot and take the write lock only to write.

The pyproject filter turns WriteBeforeModelCall into an error, so every test in the
suite also checks that no transaction writes before calling the model.
"""

import sqlite3

import pytest
from test_open_tasks import FakeOpenModel

from harness.core.open_engine import OpenEngine
from harness.persistence.replay import retry_stale
from harness.persistence.storage import Snapshot, StaleSnapshot, WriteBeforeModelCall


def test_engine_database_uses_wal(tmp_path):
    engine = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())
    with engine.connect() as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_write_before_model_call_is_reported(tmp_path):
    engine = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())

    @retry_stale
    def careless():
        with engine.transaction() as c:
            c.execute("INSERT INTO learning_events(session,kind,payload,at) VALUES('x','note','{}',0)")
            engine.model.review({}, 'candidate', 'probe')

    with pytest.warns(WriteBeforeModelCall):
        careless()


def test_stale_read_snapshot_cannot_commit_but_locked_rerun_can(tmp_path):
    path = tmp_path / 'db.sqlite'
    OpenEngine(path, FakeOpenModel())
    other = sqlite3.connect(path, isolation_level=None)

    snapshot = Snapshot(sqlite3.connect(path))
    assert snapshot.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == 0
    other.execute("INSERT INTO learning_events(session,kind,payload,at) VALUES('o','note','{}',0)")
    # The open snapshot still reads the old state, and may not write on top of it.
    assert snapshot.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == 0
    with pytest.raises(StaleSnapshot):
        snapshot.execute("INSERT INTO learning_events(session,kind,payload,at) VALUES('s','note','{}',0)")
    snapshot.close()

    rerun = Snapshot(sqlite3.connect(path), locked=True)
    rerun.execute("INSERT INTO learning_events(session,kind,payload,at) VALUES('s','note','{}',0)")
    assert rerun.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == 2  # sees its own write
    rerun.commit()
    rerun.close()
    assert other.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == 2


def test_read_only_and_rolled_back_transactions_leave_no_trace(tmp_path):
    engine = OpenEngine(tmp_path / 'db.sqlite', FakeOpenModel())

    @retry_stale
    def failing():
        with engine.transaction() as c:
            c.execute("INSERT INTO learning_events(session,kind,payload,at) VALUES('x','note','{}',0)")
            raise ValueError('boom')

    with pytest.raises(ValueError):
        failing()
    with engine.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM learning_events').fetchone()[0] == 0
