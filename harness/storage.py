"""Compute on a SQLite snapshot; hold a write lock only while committing.
Conservative database-wide optimistic conflicts protect cross-session evidence reads.
"""
import sqlite3
from .errors import Conflict


class Snapshot:
    def __init__(self, disk):
        self.disk = disk
        self.version = disk.execute('PRAGMA data_version').fetchone()[0]
        self.local = sqlite3.connect(':memory:')
        self.local.row_factory = sqlite3.Row
        disk.backup(self.local)
        self.writes = []

    def execute(self, sql, args=()):
        result = self.local.execute(sql, args)
        if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            self.writes.append((sql, args))
        return result

    def commit(self):
        if not self.writes:
            return
        self.disk.execute('BEGIN IMMEDIATE')
        if self.disk.execute('PRAGMA data_version').fetchone()[0] != self.version:
            raise Conflict('学习记录已被其他操作更新，请刷新后重试；本轮未提交。')
        for sql, args in self.writes:
            self.disk.execute(sql, args)
        self.disk.commit()

    def close(self):
        self.local.close()
