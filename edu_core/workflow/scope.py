"""Where student work lives: one explicit scope instead of a (project, round, assignment) triple.

Student work lives in one of two worlds:

* a **class assignment** -- the question and rubric versions it was given are pinned, and the roster it
  was given is a snapshot that a later class change does not alter;
* the **project's own unbound work** (``assignment_id == ''``) -- the latest confirmed versions apply and
  the roster is the project's learner list.

Before, every stage received ``(pid, round_, assignment_id)`` and re-derived which world it was in. The
rules were scattered over ``_scoped_questions``, ``_scoped_rubric``, ``_roster``, ``_submission_coverage``,
``_add_to_roster`` and ``_basis_changed``, and each new stage method had to remember all of them. A
:class:`Scope` resolves the world once per call and answers the questions the stages actually ask.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from edu_core import artifacts as A
from edu_core.storage import new_id
from edu_core.text import dump
from edu_core.workflow.common import Conflict


@dataclass(frozen=True)
class Scope:
    """One resolution of "whose work is this?" for the duration of a call."""

    ws: object
    project_id: str
    round: int = 1
    assignment_id: str = ''
    offering_id: str = ''

    # ----- construction -----

    @classmethod
    def resolve(cls, ws, c, pid, round_, assignment_id):
        """The scope of a call: an assignment (its own round and project) or the project's own work."""
        if not assignment_id:
            if pid is None:
                raise ValueError('缺少项目')
            return cls(ws, pid, round_ or 1)
        return cls.from_row(ws, ws._assignment_row(c, assignment_id), pid=pid)

    @classmethod
    def from_row(cls, ws, a, pid=None):
        """Scope of an assignment row already loaded. ``pid`` is checked when the caller named a project."""
        if pid and a['project_id'] != pid:
            raise Conflict('这次布置不属于当前项目')
        return cls(ws, a['project_id'], a['round'], a['id'], a.get('offering_id') or '')

    # ----- what kind of work this is -----

    @property
    def is_assignment(self) -> bool:
        return bool(self.assignment_id)

    @property
    def kind(self) -> str:
        """``'assignment'`` or ``'project'``: the two worlds stated plainly for callers and exports."""
        return 'assignment' if self.assignment_id else 'project'

    def assignment(self, c):
        """The assignment row (roster snapshot and pins), or ``None`` for the project's own work."""
        return self.ws._assignment_row(c, self.assignment_id) if self.assignment_id else None

    def pins(self, c) -> dict:
        """question lineage -> the pinned question and rubric versions (empty for the project's own work)."""
        a = self.assignment(c)
        return a['pinned'] if a else {}

    # ----- the versions this work uses -----

    def questions(self, c) -> dict:
        """No -> the question version this scope grades and prints."""
        if self.assignment_id:
            out = {}
            for pin in self.pins(c).values():
                q = A.get(c, pin['question'])
                out[q['content']['no']] = q
            return out
        return {
            q['content']['no']: A.confirmed_in_lineage(c, q['lineage_id'])
            for q in A.current(c, self.project_id, 'question', round_=self.round)
            if q['confirmed_version']
        }

    def rubric(self, c, question_lineage):
        """The rubric of one question: pinned for an assignment, latest confirmed for the project's own work."""
        pin = self.pins(c).get(question_lineage) if self.assignment_id else None
        return A.get(c, pin['rubric']) if pin else self.ws._rubric_for(c, question_lineage)

    def current(self, c, type_, round_=None):
        """Latest versions in this scope. Student-work types are filtered by the assignment; content is not."""
        return A.current(
            c,
            self.project_id,
            type_,
            round_=round_,
            assignment=self.assignment_id if type_ in A.ASSIGNMENT_TYPES else None,
        )

    def listing(self, c, type_, statuses=None, round_=None):
        return A.listing(
            c,
            self.project_id,
            type_,
            statuses=statuses,
            round_=round_,
            assignment=self.assignment_id if type_ in A.ASSIGNMENT_TYPES else None,
        )

    # ----- who is in this work -----

    def roster(self, c) -> dict:
        """Alias -> learner id: the assignment's snapshot, or the project's learners."""
        if self.assignment_id:
            return {m['alias']: m['id'] for m in self.assignment(c)['roster']}
        return {
            r['alias']: r['id']
            for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (self.project_id,))
        }

    def add_learner(self, c, alias) -> str:
        """An alias outside the roster joins it: the class and this assignment's snapshot, or the project."""
        if not self.assignment_id:
            lid = new_id('lrn')
            c.execute('INSERT INTO learners VALUES(?,?,?,?)', (lid, self.project_id, alias, time.time()))
            return lid
        a = self.assignment(c)
        class_id = c.execute('SELECT class_id FROM offerings WHERE id=?', (a['offering_id'],)).fetchone()[0]
        m = c.execute('SELECT id FROM class_members WHERE class_id=? AND alias=?', (class_id, alias)).fetchone()
        mid = m[0] if m else new_id('mem')
        if not m:
            c.execute(
                'INSERT INTO class_members(id,class_id,alias,source,joined_at) VALUES(?,?,?,?,?)',
                (mid, class_id, alias, 'import', time.time()),
            )
        roster = a['roster'] + [{'id': mid, 'alias': alias}]
        c.execute('UPDATE assignments SET roster=? WHERE id=?', (dump(roster), self.assignment_id))
        return mid

    def submission_lineage(self, c, learner_id, question_lineage):
        """The lineage of this learner's answer to this question in this scope, if one was imported."""
        for a in self.listing(c, 'submission', round_=self.round):
            if a['content']['learner_id'] == learner_id and a['content']['question_lineage'] == question_lineage:
                return a['lineage_id']
        return None

    # ----- what is missing -----

    def coverage(self, c, subs) -> dict:
        """Records expected (roster x questions) against the ones received.

        A missing record is unknown, not a blank answer and not a zero; ``blank`` counts answers that were
        imported with empty text, which is a different thing.
        """
        roster, questions = self.roster(c), self.questions(c)
        expected = {(lid, q['lineage_id']) for lid in roster.values() for q in questions.values()}
        actual = {(s['content']['learner_id'], s['content']['question_lineage']) for s in subs}
        missing = expected - actual
        return {
            'expected': len(expected),
            'submitted': len(expected & actual),
            'missing': len(missing),
            'blank': sum(not s['content'].get('text', '').strip() for s in subs),
            'missing_answers': [
                {'learner_id': lid, 'learner_alias': alias, 'question_lineage': q['lineage_id'], 'question_no': no}
                for alias, lid in roster.items()
                for no, q in questions.items()
                if (lid, q['lineage_id']) in missing
            ],
        }

    # ----- did the basis change while a model ran? -----

    def basis_changed(self, c, manifest) -> list:
        """Artifacts that replaced what a model call read while it ran (design doc 8).

        For an assignment the pinned version is the basis, so a newer content version alone is not a change;
        re-pinning or correcting the rubric is.
        """
        pinned_ids = {}
        for q_lin, pin in self.pins(c).items():
            pinned_ids[q_lin] = pin['question']
            pinned_ids[pin.get('rubric_lineage')] = pin['rubric']
        changed = []
        for m in manifest:
            if m.get('kind') not in A.TYPES or 'lineage_id' not in m:
                continue
            if m['lineage_id'] in pinned_ids:
                if pinned_ids[m['lineage_id']] != m['id']:
                    changed.append(pinned_ids[m['lineage_id']])
                continue
            newer = c.execute(
                "SELECT id FROM artifacts WHERE lineage_id=? AND status='confirmed' AND version>? "
                'ORDER BY version DESC LIMIT 1',
                (m['lineage_id'], m['version']),
            ).fetchone()
            if newer:
                changed.append(newer[0])
        return changed
