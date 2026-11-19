"""Accounts and access (COURSE_WORKSPACE_DESIGN.md 10, last paragraph).

Off by default (local single-user tool). With ``EDU_AUTH=1`` every API request needs a signed-in user, and the
server checks each record a request names: belonging to a course is not authorization. A user may access:

* a course they are a member of, and everything in it (tasks, classes linked to it, assignments, students' work);
* their own projects and classes that are not in any course;
* nothing else, unless they are an admin. Records created before accounts existed (no owner) are admin-only.

Tokens are random, shown once, and stored only as SHA-256.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from edu_core.access import CURRENT_USER, is_admin
from edu_core.storage import event, new_id, row
from edu_core.workflow.common import Conflict


def _token_sha(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


class AccountsMixin:
    """Users, course membership and per-record access checks."""

    def create_user(self, name, role='teacher'):
        name = (name or '').strip()
        if not name or len(name) > 40:
            raise ValueError('请填写 40 字以内的用户名')
        if role not in ('teacher', 'admin'):
            raise ValueError('角色应为 teacher 或 admin')
        token = 'edu_' + secrets.token_urlsafe(24)
        with self.store.tx() as c:
            if c.execute('SELECT 1 FROM users WHERE name=?', (name,)).fetchone():
                raise Conflict('用户名已存在')
            uid = new_id('usr')
            c.execute(
                'INSERT INTO users(id,name,role,token_sha,created_at) VALUES(?,?,?,?,?)',
                (uid, name, role, _token_sha(token), time.time()),
            )
            event(c, 'accounts', 'user_created', {'user': uid, 'role': role})
        return {'id': uid, 'name': name, 'role': role, 'token': token, 'note': '访问码只显示这一次，请妥善保存'}

    def users(self):
        with self.store.read() as c:
            return [
                dict(r) for r in c.execute('SELECT id, name, role, disabled, created_at FROM users ORDER BY created_at')
            ]

    def set_user_disabled(self, user_id, disabled):
        with self.store.tx() as c:
            c.execute('UPDATE users SET disabled=? WHERE id=?', (int(bool(disabled)), user_id))
        return {'ok': True}

    def authenticate(self, token):
        if not token:
            return None
        with self.store.read() as c:
            r = row(
                c.execute(
                    'SELECT id, name, role, disabled FROM users WHERE token_sha=?', (_token_sha(token),)
                ).fetchone()
            )
        return r if r and not r['disabled'] else None

    def share_course(self, course_id, user_name, role='member'):
        """Course owners (or admins) add another teacher to a course."""
        with self.store.tx() as c:
            self._course(c, course_id)
            self._require_course_owner(c, course_id)
            u = c.execute('SELECT id FROM users WHERE name=?', (user_name,)).fetchone()
            if u is None:
                raise KeyError(f'没有这个用户：{user_name}')
            c.execute(
                'INSERT OR REPLACE INTO course_members(course_id,user_id,role) VALUES(?,?,?)',
                (course_id, u[0], role if role in ('owner', 'member') else 'member'),
            )
            event(c, course_id, 'course_shared', {'user': u[0], 'role': role})
        return self.course_members(course_id)

    def course_members(self, course_id):
        with self.store.read() as c:
            return [
                dict(r)
                for r in c.execute(
                    'SELECT u.id, u.name, m.role FROM course_members m JOIN users u ON u.id=m.user_id WHERE m.course_id=?',
                    (course_id,),
                )
            ]

    def _require_course_owner(self, c, course_id):
        u = CURRENT_USER.get()
        if u is None or is_admin(u):
            return
        r = c.execute(
            'SELECT role FROM course_members WHERE course_id=? AND user_id=?', (course_id, u['id'])
        ).fetchone()
        if not r or r[0] != 'owner':
            raise Conflict('只有课程负责人可以添加成员')

    # ----- ownership on create -----

    def _record_owner(self, c, kind, ident):
        u = CURRENT_USER.get()
        if u is None:
            return
        if kind == 'course':
            c.execute(
                'INSERT OR IGNORE INTO course_members(course_id,user_id,role) VALUES(?,?,?)', (ident, u['id'], 'owner')
            )
        elif kind == 'project':
            c.execute('UPDATE projects SET owner_id=? WHERE id=?', (u['id'], ident))
        elif kind == 'class':
            c.execute('UPDATE class_groups SET owner_id=? WHERE id=?', (u['id'], ident))

    # ----- access checks -----

    def visible_course_ids(self, c, u=None):
        u = u if u is not None else CURRENT_USER.get()
        if u is None or is_admin(u):
            return None  # everything
        return {r[0] for r in c.execute('SELECT course_id FROM course_members WHERE user_id=?', (u['id'],))}

    def _course_of(self, c, kind, ident):
        """('course', id) or ('owner', user id) that governs a record; None if the record does not exist."""
        q = lambda sql: c.execute(sql, (ident,)).fetchone()  # noqa: E731
        if kind == 'course':
            return ('course', ident) if q('SELECT 1 FROM courses WHERE id=?') else None
        if kind == 'project':
            r = q('SELECT course_id, owner_id FROM projects WHERE id=?')
            return None if r is None else (('course', r[0]) if r[0] else ('owner', r[1]))
        if kind == 'artifact':
            r = q('SELECT project_id FROM artifacts WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'assignment':
            r = q('SELECT project_id FROM assignments WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'offering':
            r = q('SELECT course_id FROM offerings WHERE id=?')
            return r and ('course', r[0])
        if kind == 'session':
            r = q('SELECT offering_id FROM lesson_sessions WHERE id=?')
            return r and self._course_of(c, 'offering', r[0])
        if kind == 'unit':
            r = q('SELECT course_id FROM units WHERE id=?')
            return r and ('course', r[0])
        if kind == 'objective':
            r = q('SELECT course_id FROM objectives WHERE id=?')
            return r and ('course', r[0])
        if kind == 'task':
            r = q('SELECT project_id FROM tasks WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'stale':
            r = q('SELECT project_id FROM stale_marks WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'choice':
            r = q('SELECT project_id FROM choices WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'case':
            r = q('SELECT project_id FROM research_cases WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'consent':
            r = q('SELECT course_id FROM consents WHERE id=?')
            return r and ('course', r[0])
        if kind == 'package':
            r = q('SELECT project_id FROM packages WHERE id=?')
            return r and self._course_of(c, 'project', r[0])
        if kind == 'group':
            r = q('SELECT project_id FROM generations WHERE candidate_group=? LIMIT 1')
            return r and self._course_of(c, 'project', r[0])
        raise ValueError(f'unknown record kind {kind}')

    def can_access(self, u, kind, ident):
        """May user ``u`` access this record? Missing records answer True so the handler can return 404."""
        if u is None or is_admin(u):
            return True
        with self.store.read() as c:
            if kind == 'class':
                r = c.execute('SELECT owner_id FROM class_groups WHERE id=?', (ident,)).fetchone()
                if r is None:
                    return True
                if r[0] == u['id']:
                    return True
                courses = self.visible_course_ids(c, u)
                return any(
                    o[0] in courses for o in c.execute('SELECT course_id FROM offerings WHERE class_id=?', (ident,))
                )
            if kind == 'photo':
                pids = [
                    r[0]
                    for r in c.execute(
                        "SELECT project_id FROM artifacts WHERE type='submission' AND content LIKE ?", (f'%{ident}%',)
                    )
                ]
                return not pids or any(self._governs(c, u, self._course_of(c, 'project', p)) for p in pids)
            return self._governs(c, u, self._course_of(c, kind, ident), missing_ok=True)

    def _governs(self, c, u, scope, missing_ok=False):
        if scope is None:
            return missing_ok
        how, ident = scope
        if how == 'course':
            return bool(
                c.execute('SELECT 1 FROM course_members WHERE course_id=? AND user_id=?', (ident, u['id'])).fetchone()
            )
        return bool(ident) and ident == u['id']

    def filter_projects(self, c, rows):
        u = CURRENT_USER.get()
        if u is None or is_admin(u):
            return rows
        courses = self.visible_course_ids(c, u)
        return [
            r
            for r in rows
            if (r.get('course_id') in courses) or (not r.get('course_id') and r.get('owner_id') == u['id'])
        ]
