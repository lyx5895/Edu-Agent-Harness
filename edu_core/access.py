"""Who is acting. Without accounts there is no user: ``CURRENT_USER`` stays None; runs record it when set."""

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
