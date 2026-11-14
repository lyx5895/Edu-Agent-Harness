"""Compute inside a SQLite WAL read snapshot; take the write lock only when writing.

An operation runs in a deferred transaction on the real database: its reads see one
consistent snapshot, model calls happen without holding any lock, and its writes
come last. If another connection committed after the snapshot began, SQLite refuses
to upgrade the stale read transaction (SQLITE_BUSY_SNAPSHOT); that is surfaced as
:class:`StaleSnapshot` and the caller re-runs the computation against fresh data with
recorded model results (see ``replay.py``). Reruns start with ``BEGIN IMMEDIATE`` and so
cannot go stale; their model calls are replayed and take milliseconds.

Requirements: the database is in WAL mode (``enable_wal``), and within a transaction
no write precedes a model call (``tests/test_storage.py`` checks the whole suite), or
the write lock would be held while the model runs.

Conflicts are database-wide: any commit in between makes the snapshot stale.
"""

import sqlite3
import warnings
from contextvars import ContextVar

from harness.errors import Conflict

SQLITE_BUSY = 5
SQLITE_BUSY_SNAPSHOT = 517

# The example-course engine holds a write lock across a model call (client timeout
# 300 s) and shares the database file with the open-task engine, so a writer may
# legitimately wait that long. Open-task writes themselves only lock briefly.
SQLITE_BUSY_TIMEOUT = 310


class StaleSnapshot(Conflict):
    pass


class WriteBeforeModelCall(RuntimeWarning):
    """A model call inside a transaction that already wrote: the write lock is held while the model runs."""


_current = ContextVar('snapshot', default=None)


def check_model_call():
    """Called before every real model call; warns if the current transaction already holds the write lock."""
    snapshot = _current.get()
    if snapshot is not None and snapshot.wrote and not snapshot.locked:
        warnings.warn(
            'model call after a database write in the same transaction; move the write after the call',
            WriteBeforeModelCall,
            stacklevel=3,
        )


def enable_wal(c):
    """WAL lets a long read transaction coexist with other writers. The mode is stored in the file."""
    mode = c.execute('PRAGMA journal_mode=WAL').fetchone()[0]
    if mode.lower() != 'wal':
        raise RuntimeError(f'SQLite WAL mode unavailable (journal_mode={mode}); concurrent tutoring needs it')


class Snapshot:
    """One operation's transaction. ``locked`` reruns take the write lock up front."""

    def __init__(self, disk, locked=False):
        self.disk = disk
        self.locked = locked
        self.wrote = False
        disk.isolation_level = None  # explicit BEGIN/COMMIT; Python must not open transactions itself
        disk.execute('BEGIN IMMEDIATE' if locked else 'BEGIN')
        self._token = _current.set(self)

    def execute(self, sql, args=()):
        try:
            cursor = self.disk.execute(sql, args)
        except sqlite3.OperationalError as e:
            # Upgrading a read snapshot after someone else committed can never succeed;
            # a busy timeout while upgrading means the same once the other writer commits.
            if not self.locked and getattr(e, 'sqlite_errorcode', None) in (SQLITE_BUSY_SNAPSHOT, SQLITE_BUSY):
                raise StaleSnapshot('学习记录已被其他操作更新，请刷新后重试；本轮未提交。') from e
            raise
        if not self.wrote and sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'):
            self.wrote = True
        return cursor

    def commit(self):
        if self.disk.in_transaction:
            self.disk.execute('COMMIT')

    def close(self):
        _current.reset(self._token)
        if self.disk.in_transaction:
            self.disk.execute('ROLLBACK')
