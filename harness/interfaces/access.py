"""Classroom accounts and per-route authorization.

Two modes:

* **Local demo** (``HARNESS_ACCOUNTS_FILE`` unset): a trusted single-machine demo.
  Every check passes and no token is required.
* **Classroom** (``HARNESS_ACCOUNTS_FILE`` set): every ``/api`` route requires a
  bearer token from the accounts file and checks the account's class and role.
  Accounts JSON: ``{opaque_token: {role: teacher|student, class_id: ..., learner: ...}}``.

Every API route receives a :class:`Guard` through ``Depends`` and must call one of
its checks before touching data; ``tests/test_access.py`` asserts that every
``/api`` route depends on the guard. The checks fail closed: unreadable records,
unknown ids and legacy example-course routes are forbidden in classroom mode.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from harness.teaching import planning, teacher

FORBIDDEN = '账号无权访问此班级或学习记录'


@dataclass(frozen=True)
class Account:
    role: str
    class_id: str
    learner: str | None = None

    @property
    def is_teacher(self):
        return self.role == 'teacher'


class _AccountFile:
    """Parsed accounts file, re-read only when its modification time changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._accounts: dict[str, Account] = {}

    def lookup(self, filename, token):
        path = Path(filename)
        try:
            key = (str(path), path.stat().st_mtime_ns)
            with self._lock:
                if key != self._key:
                    self._accounts = _parse(json.loads(path.read_text(encoding='utf-8')))
                    self._key = key
                accounts = self._accounts
        except (OSError, ValueError, TypeError, AttributeError) as e:
            raise HTTPException(503, '认证配置不可用') from e
        # Compare against every token so timing does not reveal which prefix matched.
        found = None
        for known, account in accounts.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                found = account
        return found


def _parse(raw):
    accounts = {}
    for token, value in raw.items():
        if value.get('role') in ('teacher', 'student') and value.get('class_id'):
            accounts[token] = Account(value['role'], value['class_id'], value.get('learner'))
    return accounts


_ACCOUNTS = _AccountFile()


def classroom_mode():
    return bool(os.getenv('HARNESS_ACCOUNTS_FILE'))


def current_account(request: Request) -> Account | None:
    """The authenticated account, or None in local-demo mode."""
    filename = os.getenv('HARNESS_ACCOUNTS_FILE')
    if not filename:
        return None
    header = request.headers.get('authorization', '')
    token = header.removeprefix('Bearer ').strip() if header.startswith('Bearer ') else ''
    account = _ACCOUNTS.lookup(filename, token) if token else None
    if account is None:
        raise HTTPException(401, '需要有效账号')
    return account


def forbid():
    raise HTTPException(403, FORBIDDEN)


class Guard:
    """Authorization checks for one request. In local-demo mode every check passes."""

    def __init__(self, account: Account | None, gateway):
        self.account = account
        self.gateway = gateway

    @property
    def open(self):
        return self.gateway.open

    def any(self):
        """Any authenticated account (or the local demo)."""

    def local_only(self):
        """Global and example-course routes have no class scope, so classroom mode forbids them."""
        if self.account:
            forbid()

    def any_teacher(self):
        """Any teacher, for class-independent tools (knowledge library, algebra preview)."""
        if self.account and not self.account.is_teacher:
            forbid()

    def teacher(self, class_id):
        """A teacher of exactly this class; a missing class never matches."""
        a = self.account
        if a and (not a.is_teacher or class_id != a.class_id):
            forbid()

    def member(self, class_id, learner):
        """The class's teacher, or the named learner of that class."""
        a = self.account
        if a and (class_id != a.class_id or (not a.is_teacher and learner != a.learner)):
            forbid()

    def session(self, sid, *, teacher_only=False):
        """An open-task session of the account's class; students only see their own."""
        if not self.account:
            return
        if not sid.startswith('open_'):
            forbid()
        try:
            with self.open.connect() as c:
                owner, state = self.open.load(c, sid)
        except ValueError:
            forbid()
        if teacher_only:
            self.teacher(state.get('scope', {}).get('class_id'))
        else:
            self.member(state.get('scope', {}).get('class_id'), owner)

    def new_session(self, learner, assignment_id, followup_id, skill):
        """Classroom sessions must come from an assignment or follow-up of the account's class."""
        if not self.account:
            return
        if skill:
            forbid()
        try:
            with self.open.connect() as c:
                if followup_id:
                    followup = planning.load_followup(c, followup_id)
                    if followup['learner'] != learner:
                        forbid()
                    assignment_id = followup['body']['assignment_id']
                assignment = teacher.load(c, assignment_id or '')
        except (ValueError, KeyError, TypeError):
            forbid()
        self.member(assignment['class_id'], learner)

    def pack(self, pack_id):
        if not self.account:
            return
        try:
            pack = self.open.load_pack(pack_id)
        except (ValueError, KeyError):
            forbid()
        self.teacher(pack['class_id'])

    def path(self, path_id, learner):
        if not self.account:
            return
        try:
            with self.open.connect() as c:
                body = planning.load_path(c, path_id)
        except (ValueError, KeyError, TypeError):
            forbid()
        self.member(body['class_id'], learner)

    def followup(self, key):
        if not self.account:
            return
        try:
            with self.open.connect() as c:
                followup = planning.load_followup(c, key)
        except (ValueError, KeyError, TypeError):
            forbid()
        self.teacher(followup['class_id'])
