"""Artifacts: immutable versions, upstream references, confirmation and staleness (design doc 5.1, 5.3).

Rules:
* Content never changes after creation; an edit creates a new version in the same lineage.
* ``confirm`` adopts a version. The previously confirmed version of that lineage becomes ``superseded``.
* If the change is ``substantive``, every artifact that referenced an older version of the lineage gets
  an open stale mark, and derived results pass it on downstream. Teacher-confirmed content (objectives,
  questions, rubrics) is flagged but does not pass it on; student submissions are never flagged.
  ``cosmetic`` changes only get recorded.
* Downstream artifacts stay pinned to the versions they used; nothing is silently regenerated.
"""

import time

from edu_core.storage import event, new_id, row
from edu_core.text import dump, sha

TYPES = {
    'objective': '教学目标',
    'question': '题目',
    'rubric': '评分标准',
    'submission': '学生作答',
    'grading': '批改',
    'analysis': '学情分析',
    'problem_pattern': '问题模式',
    'comparison': '两轮对比',
}
STATUSES = ('candidate', 'draft', 'confirmed', 'superseded')
# Student work never becomes stale: it does not depend on how we graded it.
NEVER_STALE = {'submission'}
# Teacher-confirmed content can be flagged ("its basis changed") but the flag stops there; only derived
# results (gradings, analyses, patterns, comparisons) carry it further downstream.
STOP_PROPAGATION = {'objective', 'question', 'rubric'}


class Conflict(ValueError):
    """The request conflicts with the current state (wrong status, stale version...)."""


def get(c, artifact_id):
    a = row(c.execute('SELECT * FROM artifacts WHERE id=?', (artifact_id,)).fetchone())
    if a is None:
        raise KeyError(f'产物不存在：{artifact_id}')
    a['refs'] = [dict(r) for r in c.execute('SELECT * FROM artifact_refs WHERE artifact_id=?', (artifact_id,))]
    return a


def create(
    c,
    project_id,
    type_,
    content,
    *,
    origin,
    refs=(),
    generation_id=None,
    lineage_id=None,
    status='draft',
    created_by='model',
    round_=1,
    edit_of=None,
    change_kind=None,
):
    if type_ not in TYPES:
        raise ValueError(f'未知产物类型：{type_}')
    if status not in STATUSES:
        raise ValueError(f'未知状态：{status}')
    lineage_id = lineage_id or new_id('lin')
    version = (c.execute('SELECT MAX(version) FROM artifacts WHERE lineage_id=?', (lineage_id,)).fetchone()[0] or 0) + 1
    aid = new_id(type_[:4])
    c.execute(
        'INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            aid,
            project_id,
            type_,
            lineage_id,
            version,
            status,
            dump(content),
            sha(content),
            origin,
            generation_id,
            edit_of,
            created_by,
            change_kind,
            round_,
            time.time(),
        ),
    )
    for ref_id, role in refs:
        target = c.execute('SELECT content_sha FROM artifacts WHERE id=?', (ref_id,)).fetchone()
        if target is None:
            raise KeyError(f'引用的产物不存在：{ref_id}')
        c.execute('INSERT OR IGNORE INTO artifact_refs VALUES(?,?,?,?)', (aid, ref_id, target[0], role))
    if status == 'confirmed':
        _supersede_previous(c, project_id, lineage_id, aid, change_kind or 'substantive')
    return get(c, aid)


def edit(c, artifact_id, content, *, teacher='teacher'):
    """A teacher edit: new draft version in the same lineage, keeping the source's upstream references."""
    src = get(c, artifact_id)
    if src['status'] == 'superseded':
        raise Conflict('该版本已被取代，请在最新版本上修改')
    return create(
        c,
        src['project_id'],
        src['type'],
        content,
        origin='edited',
        refs=[(r['ref_id'], r['role']) for r in src['refs']],
        lineage_id=src['lineage_id'],
        status='draft',
        created_by=teacher,
        round_=src['round'],
        edit_of=artifact_id,
    )


def confirm(c, artifact_id, *, change_kind='substantive', teacher='teacher'):
    if change_kind not in ('substantive', 'cosmetic'):
        raise ValueError('修改性质应为 substantive（实质修改）或 cosmetic（文字修改）')
    a = get(c, artifact_id)
    if a['status'] == 'confirmed':
        return a
    if a['status'] == 'superseded':
        raise Conflict('该版本已被取代，不能再确认')
    c.execute('UPDATE artifacts SET status=?, change_kind=? WHERE id=?', ('confirmed', change_kind, artifact_id))
    _supersede_previous(c, a['project_id'], a['lineage_id'], artifact_id, change_kind)
    event(c, a['project_id'], 'confirm', {'artifact_id': artifact_id, 'change_kind': change_kind, 'teacher': teacher})
    return get(c, artifact_id)


def _supersede_previous(c, project_id, lineage_id, new_id_, change_kind):
    old = [
        r[0]
        for r in c.execute(
            "SELECT id FROM artifacts WHERE lineage_id=? AND id!=? AND status IN ('confirmed','draft')",
            (lineage_id, new_id_),
        )
    ]
    had_confirmed = c.execute(
        "SELECT COUNT(*) FROM artifacts WHERE lineage_id=? AND id!=? AND status='confirmed'", (lineage_id, new_id_)
    ).fetchone()[0]
    if old:
        c.execute(f'UPDATE artifacts SET status=? WHERE id IN ({",".join("?" * len(old))})', ('superseded', *old))
    if had_confirmed and change_kind == 'substantive':
        mark_downstream(c, project_id, lineage_id, new_id_)


def mark_downstream(c, project_id, lineage_id, cause_id):
    """Open stale marks on everything that used an older version of ``lineage_id``, transitively."""
    older = [r[0] for r in c.execute('SELECT id FROM artifacts WHERE lineage_id=? AND id!=?', (lineage_id, cause_id))]
    frontier, seen = list(older), set(older)
    while frontier:
        marks = ','.join('?' * len(frontier))
        users = c.execute(
            f'SELECT DISTINCT r.artifact_id, a.type FROM artifact_refs r JOIN artifacts a ON a.id=r.artifact_id '
            f"WHERE r.ref_id IN ({marks}) AND a.status!='superseded'",
            frontier,
        ).fetchall()
        frontier = []
        for user, kind in users:
            if user in seen or kind in NEVER_STALE:
                continue
            seen.add(user)
            if kind not in STOP_PROPAGATION:
                frontier.append(user)
            exists = c.execute("SELECT 1 FROM stale_marks WHERE artifact_id=? AND status='open'", (user,)).fetchone()
            if not exists:
                c.execute(
                    'INSERT INTO stale_marks(project_id,artifact_id,cause_id,created_at) VALUES(?,?,?,?)',
                    (project_id, user, cause_id, time.time()),
                )


def stale(c, project_id):
    rows = c.execute(
        'SELECT s.*, a.type, a.version, a.lineage_id, a.round FROM stale_marks s JOIN artifacts a ON a.id=s.artifact_id '
        "WHERE s.project_id=? AND s.status='open' AND a.status!='superseded' ORDER BY s.id",
        (project_id,),
    )
    out = []
    for r in rows:
        d = dict(r)
        cause = c.execute('SELECT type, version FROM artifacts WHERE id=?', (d['cause_id'],)).fetchone()
        d['cause'] = {'type': cause['type'], 'version': cause['version']} if cause else None
        out.append(d)
    return out


def resolve_stale(c, mark_id, resolution):
    if resolution not in ('kept', 'redone'):
        raise ValueError('处理方式应为 kept（保留原结果）或 redone（已重新生成）')
    c.execute(
        "UPDATE stale_marks SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
        (resolution, time.time(), mark_id),
    )


def is_stale(c, artifact_id):
    return (
        c.execute("SELECT 1 FROM stale_marks WHERE artifact_id=? AND status='open'", (artifact_id,)).fetchone()
        is not None
    )


def listing(c, project_id, type_=None, *, statuses=None, round_=None):
    sql, args = 'SELECT * FROM artifacts WHERE project_id=?', [project_id]
    if type_:
        sql += ' AND type=?'
        args.append(type_)
    if statuses:
        sql += f' AND status IN ({",".join("?" * len(statuses))})'
        args += list(statuses)
    if round_ is not None:
        sql += ' AND round=?'
        args.append(round_)
    sql += ' ORDER BY created_at'
    return [row(r) for r in c.execute(sql, args)]


def current(c, project_id, type_, **kw):
    """Latest non-superseded version per lineage, with the lineage's confirmed version number attached."""
    best, confirmed = {}, {}
    for a in listing(c, project_id, type_, statuses=('draft', 'confirmed', 'candidate'), **kw):
        if a['status'] == 'confirmed':
            confirmed[a['lineage_id']] = a['version']
        prev = best.get(a['lineage_id'])
        if prev is None or a['version'] > prev['version']:
            best[a['lineage_id']] = a
    for a in best.values():
        a['confirmed_version'] = confirmed.get(a['lineage_id'])
        a['stale'] = is_stale(c, a['id'])
    return sorted(best.values(), key=lambda a: a['created_at'])


def confirmed_in_lineage(c, lineage_id):
    return row(
        c.execute(
            "SELECT * FROM artifacts WHERE lineage_id=? AND status='confirmed' ORDER BY version DESC LIMIT 1",
            (lineage_id,),
        ).fetchone()
    )
