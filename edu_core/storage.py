"""SQLite storage (design doc 10). Model calls never run inside a transaction: read, call, then write."""

import contextlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from edu_core.text import dump

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL, grade TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS learners(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, alias TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(project_id, alias));
CREATE TABLE IF NOT EXISTS materials(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, family_id TEXT NOT NULL, kind TEXT NOT NULL,
  filename TEXT NOT NULL, sha256 TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL, lineage_id TEXT NOT NULL,
  version INTEGER NOT NULL, status TEXT NOT NULL, content TEXT NOT NULL, content_sha TEXT NOT NULL,
  origin TEXT NOT NULL, generation_id TEXT, edit_of TEXT, created_by TEXT NOT NULL,
  change_kind TEXT, round INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
  UNIQUE(lineage_id, version));
CREATE INDEX IF NOT EXISTS artifact_project ON artifacts(project_id, type, status);
CREATE TABLE IF NOT EXISTS artifact_refs(
  artifact_id TEXT NOT NULL, ref_id TEXT NOT NULL, ref_sha TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY(artifact_id, ref_id, role));
CREATE INDEX IF NOT EXISTS artifact_ref_target ON artifact_refs(ref_id);
CREATE TABLE IF NOT EXISTS stale_marks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
  cause_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', resolution TEXT,
  created_at REAL NOT NULL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS generations(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task TEXT NOT NULL, role TEXT NOT NULL,
  model_requested TEXT NOT NULL, model_returned TEXT, prompt_version TEXT NOT NULL,
  context_manifest TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, usage TEXT NOT NULL,
  latency_ms INTEGER, candidate_group TEXT, position TEXT, variant TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS choices(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, candidate_group TEXT NOT NULL, verdict TEXT NOT NULL,
  chosen_position TEXT, reason TEXT NOT NULL DEFAULT '', teacher TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, at REAL NOT NULL);
"""


# Schema revisions, applied in order and recorded in PRAGMA user_version. SQLite cannot drop or reorder
# columns, so a column is added by a new revision and never edited in place. A fresh database runs every
# revision too, so there is one definition of the schema instead of two.
MIGRATIONS = {
    1: """
ALTER TABLE projects ADD COLUMN knowledge_module TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN lesson TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN learner_profile TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN prev_project_id TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN carryover TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN lesson_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN curriculum TEXT NOT NULL DEFAULT '';
""",
}
SCHEMA_VERSION = max(MIGRATIONS)


def migrate(c) -> None:
    """Bring an existing database up to SCHEMA_VERSION (newer databases are left alone)."""
    version = c.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        return  # written by a newer build: do not touch it
    for target in range(version + 1, SCHEMA_VERSION + 1):
        c.execute('BEGIN IMMEDIATE')
        try:
            for statement in filter(None, (s.strip() for s in MIGRATIONS[target].split(';'))):
                # Databases from before user_version may already have the column (the old startup
                # added columns one by one): skip it instead of failing with "duplicate column".
                m = re.fullmatch(r'ALTER TABLE (\w+) ADD COLUMN (\w+) .*', statement, re.S)
                if m and m.group(2) in {r[1] for r in c.execute(f'PRAGMA table_info({m.group(1)})')}:
                    continue
                c.execute(statement)
            c.execute(f'PRAGMA user_version={target}')
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:16]}'


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = self.connect()
        try:
            c.executescript(SCHEMA)  # executescript manages its own transaction
            migrate(c)
        finally:
            c.close()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @contextmanager
    def tx(self):
        """Write transaction (BEGIN IMMEDIATE). Keep it short and never call a model inside it."""
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
        finally:
            c.close()

    @contextmanager
    def read(self):
        c = self.connect()
        try:
            yield c
        finally:
            c.close()


def event(c, project_id, kind, payload):
    c.execute(
        'INSERT INTO events(project_id,kind,payload,at) VALUES(?,?,?,?)', (project_id, kind, dump(payload), time.time())
    )


def record_generation(c, project_id, meta, manifest, prompt_version, *, group=None, position=None, variant=None):
    gid = new_id('gen')
    c.execute(
        'INSERT INTO generations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            gid,
            project_id,
            meta['task'],
            meta['role'],
            meta['model_requested'],
            meta.get('model_returned'),
            prompt_version,
            dump(manifest),
            dump(meta.get('request', [])),
            meta.get('response', ''),
            dump(meta.get('usage', {})),
            meta.get('latency_ms'),
            group,
            position,
            variant,
            time.time(),
        ),
    )
    return gid


def row(r):
    if r is None:
        return None
    d = dict(r)
    for key in ('content', 'context_manifest', 'usage', 'payload', 'request'):
        if key in d and isinstance(d[key], str):
            with contextlib.suppress(ValueError):
                d[key] = json.loads(d[key])
    return d
