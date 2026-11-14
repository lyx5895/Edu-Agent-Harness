"""Teacher assignments, reports, reviews and evidence invalidation.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""

import hashlib
import json

from harness.core import dependencies
from harness.core.evidence import dump, review_risks
from harness.core.memory import summarize
from harness.core.tasks import POLICIES
from harness.errors import Conflict
from harness.persistence.replay import retry_stale
from harness.teaching import planning, teacher


class TeacherMixin:
    def traces(self, sid):
        with self.connect() as c:
            self.load(c, sid)
            return [
                dict(json.loads(r['payload']), event_id=r['id'])
                for r in c.execute(
                    "SELECT id,payload FROM learning_events WHERE session=? AND kind='trace' ORDER BY id", (sid,)
                )
            ]

    def replay(self, sid):
        checks = []
        for t in self.traces(sid):
            if 'input' not in t:
                continue
            decision = POLICIES.get(t['policy_version'], lambda _: None)(t['input'])
            if t['operation'] == 'resume_practice':
                decision = {'action': 'resume_practice', 'reason': '学生选择回到原任务继续学习。'}
            checks.append({'event_id': t['event_id'], 'matches': decision == t['decision']})
        return {
            'passed': all(t['matches'] for t in checks),
            'turns': checks,
            'model_called': False,
            'scope': 'deterministic policy replay, not regeneration of model content',
        }

    def report(self, learner=None):
        with self.connect() as c:
            sessions = c.execute(
                'SELECT * FROM learning_sessions' + (' WHERE learner=?' if learner else ''),
                (learner,) if learner else (),
            ).fetchall()
            reports = []
            for row in sessions:
                turns = []
                corrections = {}
                events = c.execute('SELECT * FROM learning_events WHERE session=? ORDER BY id', (row['id'],)).fetchall()
                for e in events:
                    p = json.loads(e['payload'])
                    if e['kind'] == 'correction':
                        corrections[p['event_id']] = p
                for e in events:
                    p = json.loads(e['payload'])
                    if e['kind'] == 'turn':
                        p['event_id'] = e['id']
                        if e['id'] in corrections:
                            p['original_status'] = p.get('status')
                            p['status'] = corrections[e['id']]['status']
                            p['original_evidence'] = p['evidence']
                            p['evidence'] = 'teacher_revised_needs_recheck'
                            p['correction'] = corrections[e['id']]
                        turns.append(p)
                s = json.loads(row['state'])
                reports.append(
                    {
                        'session_id': row['id'],
                        'learner': row['learner'],
                        'task': s['task'],
                        'needs_review': s['needs_review'],
                        'turns': turns,
                    }
                )
            return reports

    @retry_stale
    def correct(self, sid, event_id, status, reason, key):
        if status not in ('correct', 'incorrect', 'incomplete', 'unknown') or not reason.strip():
            raise ValueError('需有效判定及依据')
        with self.transaction() as c:
            digest, old = self.cached(c, sid, key, ['correction', event_id, status, reason])
            if old:
                return old
            _, s = self.load(c, sid)
            r = c.execute(
                "SELECT payload FROM learning_events WHERE id=? AND session=? AND kind='turn'", (event_id, sid)
            ).fetchone()
            if not r or 'status' not in json.loads(r['payload']):
                raise ValueError('找不到可修订的作答')
            result = {'event_id': event_id, 'status': status, 'reason': reason}
            self.event(c, sid, 'correction', result)
            s['needs_review'] = True
            s['version'] += 1
            c.execute('UPDATE learning_sessions SET state=? WHERE id=?', (dump(s), sid))
            affected = {sid}
            for consumer in sorted(dependencies.downstream(c, sid)):
                _, state = self.load(c, consumer)
                if not state['needs_review']:
                    state['needs_review'] = True
                    state['version'] += 1
                    c.execute('UPDATE learning_sessions SET state=? WHERE id=?', (dump(state), consumer))
                self.event(c, consumer, 'memory_invalidated', {'source_session': sid, 'source_event': event_id})
                affected.add(consumer)
            planning.invalidate(c, affected)
            result['invalidated_sessions'] = sorted(affected)
            self.remember(c, sid, key, digest, result)
            return result

    def publish_assignment(self, data):
        self.check_knowledge_ids([data.get('concept_id')] + [c.get('concept_id') for c in data.get('criteria') or []])
        with self.transaction() as c:
            return teacher.publish(c, data)

    def assignments(self, class_id):
        with self.connect() as c:
            return teacher.list_for_class(c, class_id)

    def classes(self):
        """Every class name in use. Classes are labels, not records: a new name starts a new class."""
        with self.connect() as c:
            rows = c.execute(
                """SELECT json_extract(body,'$.class_id') FROM teaching_assignments
                   UNION SELECT class_id FROM teaching_followups
                   UNION SELECT json_extract(state,'$.scope.class_id') FROM learning_sessions"""
            )
            return sorted({r[0] for r in rows if r[0]})

    def learning_memory(self, learner, class_id=None):
        with self.connect() as c:
            return summarize(c, learner, {'class_id': class_id} if class_id else None)

    def review_detail(self, sid, event_id):
        """Read the task snapshot belonging to this attempt, even after a new check."""
        with self.connect() as c:
            learner, state = self.load(c, sid)
            row = c.execute(
                "SELECT payload FROM learning_events WHERE session=? AND id=? AND kind='turn'", (sid, event_id)
            ).fetchone()
            if not row:
                raise ValueError('找不到这份作答，请刷新列表后重试')
            p = json.loads(row['payload'])
            decisions = [
                {'kind': r['kind'], **json.loads(r['payload'])}
                for r in c.execute(
                    "SELECT kind,payload FROM learning_events WHERE session=? AND kind IN ('correction','review_confirmed') ORDER BY id",
                    (sid,),
                )
                if json.loads(r['payload'])['event_id'] == event_id
            ]
            trace = c.execute(
                "SELECT payload FROM learning_events WHERE session=? AND kind='trace' AND json_extract(payload,'$.evidence_event_id')=?",
                (sid, event_id),
            ).fetchone()
            image_id = json.loads(trace['payload']).get('context', {}).get('image_evidence_id') if trace else None
            return {
                'session_id': sid,
                'event_id': event_id,
                'learner': learner,
                'scope': state.get('scope', {}),
                'task': p.get('task', {}),
                'text': p.get('text', ''),
                'status': p.get('status'),
                'observation': p.get('assessment', {}).get('observation', ''),
                'feedback': p.get('feedback', ''),
                'criteria_results': p.get('criteria_results', []),
                'image_event_id': image_id,
                'decisions': decisions,
                'needs_review': state['needs_review'],
                'can_confirm': not decisions
                and not state['needs_review']
                and p.get('status') in ('correct', 'incorrect', 'incomplete'),
                'can_correct': not decisions and p.get('status') is not None,
            }

    def dashboard(self, class_id=None):
        with self.connect() as c:
            rows = self._learning_rows(c, class_id)
            pending = self._review_queue(c, class_id)
            groups = _support_groups(rows)
            return {
                'learning': rows,
                'review_queue': pending[:100],
                'support_groups': groups,
                'followups': planning.list_followups(c, class_id),
                'limits': '教师工作台；证据状态不是掌握概率；复习间隔按连续独立通过次数取1/3/7/14/30天，是规则建议而非遗忘模型。',
            }

    @staticmethod
    def _learning_rows(c, class_id):
        if class_id:
            learners = c.execute(
                "SELECT DISTINCT learner FROM learning_sessions WHERE json_extract(state,'$.scope.class_id')=?",
                (class_id,),
            )
        else:
            learners = c.execute('SELECT DISTINCT learner FROM learning_sessions')
        scope = {'class_id': class_id} if class_id else None
        return [
            {'learner': learner, **memory}
            for (learner,) in learners.fetchall()
            for memory in summarize(c, learner, scope)
        ]

    @staticmethod
    def _review_queue(c, class_id):
        """Undecided attempts that need a teacher: uncertain, risky, or in the 10% audit sample."""
        decided = {
            (r[0], r[1])
            for r in c.execute(
                "SELECT session, json_extract(payload,'$.event_id') FROM learning_events "
                "WHERE kind IN ('correction','review_confirmed')"
            )
        }
        query = (
            'SELECT e.id,e.session,e.payload,s.learner FROM learning_events e JOIN learning_sessions s ON e.session=s.id '
            "WHERE e.kind='turn'"
            + (" AND json_extract(s.state,'$.scope.class_id')=?" if class_id else '')
            + ' ORDER BY e.id DESC'
        )
        pending = []
        for row in c.execute(query, (class_id,) if class_id else ()):
            if (row['session'], row['id']) in decided:
                continue
            p = json.loads(row['payload'])
            urgent = (
                p.get('status') == 'unknown'
                or p.get('grading_conflict')
                or p.get('rubric_conflict')
                or p.get('delivered_action') in ('clarify', 'teacher_review')
            )
            risks = review_risks(p) if p.get('source') == 'model_rubric' else []
            sampled = (
                p.get('source') == 'model_rubric'
                and int(hashlib.sha256(f'{row["session"]}:{row["id"]}'.encode()).hexdigest(), 16) % 10 == 0
            )
            if urgent or risks or sampled:
                pending.append(
                    {
                        'session_id': row['session'],
                        'event_id': row['id'],
                        'learner': row['learner'],
                        'question': p.get('task', {}).get('question', ''),
                        'status': p.get('status'),
                        'text': p.get('text'),
                        'feedback': p.get('feedback'),
                        'priority': 'urgent' if urgent else 'risk' if risks else 'sample',
                        'risks': risks,
                        'criteria_results': p.get('criteria_results', []),
                    }
                )
        pending.sort(key=lambda p: ({'urgent': 0, 'risk': 1}.get(p['priority'], 2), -len(p['risks'])))
        return pending

    @retry_stale
    def confirm_review(self, sid, event_id, reason, request_id):
        if not reason.strip():
            raise ValueError('请填写复核依据')
        with self.transaction() as c:
            digest, old = self.cached(c, sid, request_id, ['review', event_id, reason])
            if old is not None:
                return old
            _, s = self.load(c, sid)
            row = c.execute(
                "SELECT payload FROM learning_events WHERE session=? AND id=? AND kind='turn'", (sid, event_id)
            ).fetchone()
            if not row:
                raise ValueError('找不到作答')
            payload = json.loads(row['payload'])
            if s['needs_review'] or payload.get('status') not in ('correct', 'incorrect', 'incomplete'):
                raise Conflict('失效或不确定判定需要修订，不能直接确认')
            result = {'event_id': event_id, 'reason': reason, 'status': payload['status']}
            self.event(c, sid, 'review_confirmed', result)
            self.remember(c, sid, request_id, digest, result)
            return result


def _support_groups(rows):
    """Learners stuck on the same goal and the same failed criterion, as remediation group suggestions."""
    groups = {}
    for row in rows:
        if row['state'] not in ('needs_support', 'needs_recheck'):
            continue
        last = row['evidence'][-1] if row['evidence'] else {}
        failures = [x for x in last.get('criteria', []) if x['status'] != 'correct'] or [
            {'id': 'overall', 'description': '整体作答需复查'}
        ]
        for failure in failures:
            scope = row['scope']
            key = (
                scope.get('class_id'),
                scope.get('course_id'),
                scope.get('concept_id', row['goal_id']),
                failure['id'],
            )
            group = groups.setdefault(
                key,
                {
                    'scope': scope,
                    'objective': row['objective'],
                    'criterion': failure['description'],
                    'learners': [],
                    'examples': [],
                },
            )
            group['learners'].append(row['learner'])
            if last:
                group['examples'].append(last)
    return list(groups.values())
