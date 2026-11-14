"""Compute on a SQLite snapshot; hold a write lock only while committing.
Conservative database-wide optimistic conflicts protect cross-session evidence reads;
callers re-run the computation (with replayed model results) instead of failing.
"""
import sqlite3
from harness.errors import Conflict


class StaleSnapshot(Conflict):
    pass


class Snapshot:
    def __init__(self, disk, locked=False):
        # locked: compute inside a write transaction on disk so the result cannot go stale.
        # Used for reruns, whose model calls are replayed and therefore take milliseconds.
        self.disk = disk
        self.locked = locked
        self.writes = []
        if locked:
            disk.execute('BEGIN IMMEDIATE')
            self.local = disk
            return
        self.version = disk.execute('PRAGMA data_version').fetchone()[0]
        self.local = sqlite3.connect(':memory:')
        self.local.row_factory = sqlite3.Row
        disk.backup(self.local)

    def execute(self, sql, args=()):
        result = self.local.execute(sql, args)
        if not self.locked and sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            self.writes.append((sql, args))
        return result

    def commit(self):
        if self.locked:
            self.disk.commit()
            return
        if not self.writes:
            return
        self.disk.execute('BEGIN IMMEDIATE')
        if self.disk.execute('PRAGMA data_version').fetchone()[0] != self.version:
            raise StaleSnapshot('学习记录已被其他操作更新，请刷新后重试；本轮未提交。')
        for sql, args in self.writes:
            self.disk.execute(sql, args)
        self.disk.commit()

    def close(self):
        if not self.locked:
            self.local.close()
