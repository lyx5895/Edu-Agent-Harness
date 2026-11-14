"""Typed shape of the persisted open-task session state.

The state is stored as JSON in ``learning_sessions.state``. These TypedDicts do not
change the stored format; they document every key in one place so that type
checkers and editors catch misspelled keys instead of silently reading ``None``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

# Task snapshots carry many optional, versioned fields (rubric, criteria, algebra
# bindings, provenance ...). They are validated where they are created, so here
# they stay an open mapping.
Task = dict[str, Any]
Source = dict[str, Any]


class Scope(TypedDict, total=False):
    class_id: str
    course_id: str
    concept_id: str


class PendingImage(TypedDict, total=False):
    text: str
    purpose: Literal['task', 'work']


class SessionState(TypedDict):
    version: int
    mode: Literal['problem', 'learn']
    request: str
    sources: list[Source]
    task: Task | None
    original: Task | None
    pending_image: PendingImage | None
    pending_question: str | None
    assisted: bool
    failures: int
    hypotheses: list[dict[str, Any]]
    completed: bool
    needs_review: bool
    seen: list[str]
    scope: Scope
    followup_id: str | None
    assignment_id: str | None
    teaching_note: str
    allow_explanation: bool
    # Added lazily by later operations; absent in sessions created by older versions.
    original_sources: NotRequired[list[Source]]
    stalls: NotRequired[int]
    feedback_fallbacks: NotRequired[int]
    hints_by_task: NotRequired[dict[str, int]]


def new_state(
    *,
    mode: Literal['problem', 'learn'],
    request: str,
    sources: list[Source],
    assignment: dict[str, Any] | None,
    assignment_id: str | None,
    followup_id: str | None,
) -> SessionState:
    """Initial state of a session; teacher assignments contribute scope and guidance."""
    return {
        'version': 0,
        'mode': mode,
        'request': request,
        'sources': sources,
        'task': None,
        'original': None,
        'pending_image': None,
        'pending_question': None,
        'assisted': False,
        'failures': 0,
        'hypotheses': [],
        'completed': False,
        'needs_review': False,
        'seen': [],
        'scope': {k: assignment[k] for k in ('class_id', 'course_id', 'concept_id')} if assignment else {},
        'followup_id': followup_id,
        'assignment_id': assignment_id,
        'teaching_note': assignment.get('teaching_note', '') if assignment else '',
        'allow_explanation': assignment.get('allow_explanation', True) if assignment else True,
    }


def start_task(state: SessionState, task: Task) -> None:
    """Make ``task`` the current and original task; the original is what checks are derived from."""
    state['task'] = task
    state['original'] = deepcopy(task)
    state['seen'] = [task['question']]
    state['original_sources'] = deepcopy(state['sources'])


@dataclass
class Turn:
    """Working state of one operation, shared by the per-kind handlers."""

    c: Any  # sqlite3 connection wrapped in a Snapshot
    sid: str
    learner: str
    s: SessionState
    kind: str
    body: dict[str, Any]
    text: str
    trace: dict[str, Any]
    payload: dict[str, Any]
    task: Task | None = None
    ctx: dict[str, Any] = field(default_factory=dict)
    o: dict[str, Any] = field(default_factory=dict)  # policy input
    assessment: dict[str, Any] = field(default_factory=dict)
