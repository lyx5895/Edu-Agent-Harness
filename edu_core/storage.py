"""SQLite storage (design doc 10). Model calls never run inside a transaction: read, call, then write."""

import contextlib
import contextvars
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
    # Course workspace (COURSE_WORKSPACE_DESIGN.md 10): courses, classes and their members, offerings (a class
    # taking a course), tasks, lesson sessions and assignments. Old per-lesson projects keep working; they become
    # task workspaces once a teacher maps them to a course. '' means "not linked".
    2: """
CREATE TABLE IF NOT EXISTS courses(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL, grade TEXT NOT NULL DEFAULT '',
  term TEXT NOT NULL DEFAULT '', textbook TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS class_groups(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS class_members(
  id TEXT PRIMARY KEY, class_id TEXT NOT NULL, alias TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  joined_at REAL NOT NULL, left_at REAL, UNIQUE(class_id, alias));
CREATE TABLE IF NOT EXISTS offerings(
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL, class_id TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, UNIQUE(course_id, class_id));
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL, offering_id TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL,
  kind TEXT NOT NULL, title TEXT NOT NULL, request TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS task_course ON tasks(course_id, status);
CREATE TABLE IF NOT EXISTS lesson_sessions(
  id TEXT PRIMARY KEY, offering_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL, planned_date TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
  plan_artifact_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS session_offering ON lesson_sessions(offering_id, status);
CREATE TABLE IF NOT EXISTS assignments(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, offering_id TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1,
  parent_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'assigned',
  roster TEXT NOT NULL, pinned TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS assignment_offering ON assignments(offering_id);
CREATE INDEX IF NOT EXISTS assignment_project ON assignments(project_id);
CREATE TABLE IF NOT EXISTS idempotency(
  key TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
ALTER TABLE projects ADD COLUMN course_id TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN offering_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN course_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN task_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifacts ADD COLUMN assignment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE materials ADD COLUMN course_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS artifact_assignment ON artifacts(assignment_id, type);
""",
    # Topics (单元或主题) of a course, and per-task choices about what 本班学情 to send (design doc 2.1, 7.1).
    3: """
CREATE TABLE IF NOT EXISTS units(
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL, title TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, UNIQUE(course_id, title));
ALTER TABLE projects ADD COLUMN unit_id TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN status_prefs TEXT NOT NULL DEFAULT '';
""",
    # Runs: one teacher-started model operation; every model call records its run (design doc 8, 12).
    4: """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running', error TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, finished_at REAL);
CREATE INDEX IF NOT EXISTS run_project ON runs(project_id);
ALTER TABLE generations ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
""",
    # Course objectives with versioned wording, linked from lesson plan objectives (design doc 7.4).
    5: """
CREATE TABLE IF NOT EXISTS objectives(
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL, unit_id TEXT NOT NULL DEFAULT '', code TEXT NOT NULL,
  needs_review INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, UNIQUE(course_id, code));
CREATE TABLE IF NOT EXISTS objective_versions(
  objective_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL, success_criterion TEXT NOT NULL DEFAULT '',
  source_artifact TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY(objective_id, version));
CREATE TABLE IF NOT EXISTS objective_links(
  artifact_id TEXT NOT NULL, plan_objective_id TEXT NOT NULL, objective_id TEXT NOT NULL, version INTEGER NOT NULL,
  source TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(artifact_id, plan_objective_id));
CREATE INDEX IF NOT EXISTS objective_link_target ON objective_links(objective_id);
""",
    # Phase C: A/B comparison records, per-task material choices, coursemaker packages (design doc 4.3, 9; AB spec).
    6: """
CREATE TABLE IF NOT EXISTS comparisons(
  candidate_group TEXT PRIMARY KEY, project_id TEXT NOT NULL, unit TEXT NOT NULL, input_sha TEXT NOT NULL,
  factor TEXT NOT NULL, display TEXT NOT NULL, trigger TEXT NOT NULL, sample_rate REAL NOT NULL,
  rule_version TEXT NOT NULL, ui_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'shown', created_at REAL NOT NULL);
ALTER TABLE choices ADD COLUMN work_choice TEXT NOT NULL DEFAULT '';
ALTER TABLE choices ADD COLUMN preference TEXT NOT NULL DEFAULT 'none';
ALTER TABLE choices ADD COLUMN acceptable TEXT NOT NULL DEFAULT '';
ALTER TABLE choices ADD COLUMN reason_tags TEXT NOT NULL DEFAULT '';
ALTER TABLE choices ADD COLUMN revoked_at REAL;
ALTER TABLE projects ADD COLUMN material_prefs TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS packages(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, contract TEXT NOT NULL, payload TEXT NOT NULL, sha TEXT NOT NULL,
  created_at REAL NOT NULL);
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

    def backup(self, label: str):
        """Copy the database next to itself (``<db>.bak-<label>``) with SQLite's online backup; returns the path."""
        if self.path == ':memory:':
            return None
        dest = f'{self.path}.bak-{label}'
        src = self.connect()
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return dest

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


# The run (one teacher-started operation) a model call belongs to; set by Workspace around each operation.
CURRENT_RUN: contextvars.ContextVar[str] = contextvars.ContextVar('edu_current_run', default='')

GENERATION_COLUMNS = (
    'id',
    'project_id',
    'task',
    'role',
    'model_requested',
    'model_returned',
    'prompt_version',
    'context_manifest',
    'request',
    'response',
    'usage',
    'latency_ms',
    'candidate_group',
    'position',
    'variant',
    'created_at',
    'run_id',
)


def record_generation(c, project_id, meta, manifest, prompt_version, *, group=None, position=None, variant=None):
    gid = new_id('gen')
    c.execute(
        f'INSERT INTO generations({",".join(GENERATION_COLUMNS)}) VALUES({",".join("?" * len(GENERATION_COLUMNS))})',
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
            CURRENT_RUN.get(),
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
