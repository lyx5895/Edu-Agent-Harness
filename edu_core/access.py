"""Who is acting (COURSE_WORKSPACE_DESIGN.md 10, last paragraph).

In local single-user mode there is no user: ``CURRENT_USER`` stays None and nothing is filtered. With accounts
enabled the API sets it per request; Workspace methods read it to record ownership on create and to filter lists.
Checking access to a given record is done by :func:`edu_core.workflow.accounts.AccountsMixin.can_access`, called by
the API for every route before the handler runs.
"""

from __future__ import annotations

import contextvars

CURRENT_USER: contextvars.ContextVar[dict | None] = contextvars.ContextVar('edu_current_user', default=None)


class Forbidden(PermissionError):
    """The current user may not access this record."""


def user():
    return CURRENT_USER.get()


def is_admin(u=None):
    u = u if u is not None else CURRENT_USER.get()
    return bool(u and u.get('role') == 'admin')
