"""Which sessions' evidence each session's teaching relied on.

When a turn's context imports evidence from other sessions (prior attempts, learning
memory, interventions), the consumer -> source edge is recorded here in the same
transaction. A teacher correction then invalidates every session reachable from
the corrected one by following these edges, instead of re-parsing every trace.

Databases created before this table existed are backfilled once from their traces.
"""

import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_dependencies(
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(consumer, source)
);
CREATE INDEX IF NOT EXISTS evidence_dependency_source ON evidence_dependencies(source);
CREATE TABLE IF NOT EXISTS harness_migrations(name TEXT PRIMARY KEY, at REAL NOT NULL);
"""

BACKFILL = 'evidence_dependencies_from_traces'


def referenced_sessions(context):
    """Session ids whose evidence a turn context imported."""
    refs = list(context.get('prior_task_evidence', []))
    refs += [{'session_id': x} for x in context.get('memory_dependencies', [])]
    for memory in context.get('learning_memory', []):
        refs += memory.get('evidence', []) + memory.get('interventions', [])
    return {ref['session_id'] for ref in refs if ref.get('session_id')}


def record(c, consumer, context):
    for source in referenced_sessions(context) - {consumer}:
        c.execute('INSERT OR IGNORE INTO evidence_dependencies VALUES(?,?)', (consumer, source))


def downstream(c, source):
    """Every session that transitively relied on ``source``, excluding ``source`` itself."""
    seen, frontier = {source}, [source]
    while frontier:
        marks = ','.join('?' * len(frontier))
        rows = c.execute(f'SELECT DISTINCT consumer FROM evidence_dependencies WHERE source IN ({marks})', frontier)
        frontier = [r[0] for r in rows if r[0] not in seen]
        seen.update(frontier)
    seen.discard(source)
    return seen


def initialize(c, now):
    """Create the table and, once per database, rebuild edges from existing traces."""
    c.executescript(SCHEMA)
    if c.execute('SELECT 1 FROM harness_migrations WHERE name=?', (BACKFILL,)).fetchone():
        return
    for row in c.execute("SELECT session, payload FROM learning_events WHERE kind='trace'").fetchall():
        record(c, row[0], json.loads(row[1]).get('context', {}))
    c.execute('INSERT INTO harness_migrations VALUES(?,?)', (BACKFILL, now))
    c.commit()
