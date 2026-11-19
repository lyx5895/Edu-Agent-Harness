"""Runs (COURSE_WORKSPACE_DESIGN.md 8, 12): one teacher-started model operation, such as drafting a plan or grading
an assignment. Every model call inside it (self-checks, reviews, retries of single submissions) records the run
id, so research counts can tell teacher tasks, runs and internal calls apart instead of counting every call.
"""

from __future__ import annotations

import functools
import time

from edu_core.access import CURRENT_USER
from edu_core.storage import CURRENT_RUN, new_id


def operation(kind, project_of=None):
    """Decorator for Workspace methods that call models. ``project_of(self, args, kwargs)`` returns the project id;
    by default it is the first positional argument. A nested operation joins the outer run."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if CURRENT_RUN.get():
                return fn(self, *args, **kwargs)
            pid = project_of(self, args, kwargs) if project_of else (args[0] if args else kwargs.get('pid'))
            rid = new_id('run')
            with self.store.tx() as c:
                task = c.execute('SELECT id FROM tasks WHERE project_id=? LIMIT 1', (pid,)).fetchone()
                user = CURRENT_USER.get()
                c.execute(
                    'INSERT INTO runs(id,project_id,task_id,kind,status,user_id,started_at) VALUES(?,?,?,?,?,?,?)',
                    (rid, pid or '', task[0] if task else '', kind, 'running', user['id'] if user else '', time.time()),
                )
            token = CURRENT_RUN.set(rid)
            status, error = 'succeeded', ''
            try:
                return fn(self, *args, **kwargs)
            except BaseException as e:
                status, error = 'failed', f'{type(e).__name__}: {e}'[:500]
                raise
            finally:
                CURRENT_RUN.reset(token)
                with self.store.tx() as c:
                    c.execute(
                        'UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?', (status, error, time.time(), rid)
                    )

        return wrapper

    return deco


def artifact_project(self, args, kwargs):
    """project_of for methods whose first argument is an artifact id."""
    aid = args[0] if args else next(iter(kwargs.values()))
    with self.store.read() as c:
        r = c.execute('SELECT project_id FROM artifacts WHERE id=?', (aid,)).fetchone()
    return r[0] if r else ''
