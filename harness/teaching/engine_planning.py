"""Curriculum paths, follow-up tasks and the student inbox.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""

from harness.persistence.replay import retry_stale
from harness.teaching import planning, teacher


class PlanningMixin:
    @retry_stale
    def publish_path(self, data):
        self.check_knowledge_ids([n.get('concept_id') for n in data.get('nodes') or []])
        with self.transaction() as c:
            return planning.publish_path(c, data)

    def next_step(self, path_id, learner):
        with self.connect() as c:
            return planning.next_step(c, path_id, learner)

    @retry_stale
    def assign_followups(self, data, request_id):
        with self.transaction() as c:
            digest, old = self.cached(c, 'followups:' + data['class_id'], request_id, data)
            if old is not None:
                return old
            result = planning.assign(c, data)
            self.remember(c, 'followups:' + data['class_id'], request_id, digest, result)
            return result

    def followups(self, class_id=None, learner=None):
        with self.connect() as c:
            return planning.list_followups(c, class_id, learner)

    @retry_stale
    def revise_followup(self, key, action, due_at, reason, request_id):
        if not reason.strip():
            raise ValueError('请填写调整原因')
        with self.transaction() as c:
            digest, old = self.cached(c, 'followup:' + key, request_id, [action, due_at, reason])
            if old is not None:
                return old
            result = planning.revise(c, key, action, due_at, reason)
            self.remember(c, 'followup:' + key, request_id, digest, result)
            return result

    def inbox(self, class_id, learner):
        # Do not expose answer keys, rubrics, draft packs or unseen check questions.
        with self.connect() as c:
            result = []
            for f in planning.list_followups(c, class_id, learner):
                a = teacher.load(c, f['body']['assignment_id'])
                result.append(
                    {
                        'followup_id': f['id'],
                        'course_id': a['course_id'],
                        'domain': a['domain'],
                        'objective': a['objective'],
                        'task_type': a.get('task_type', 'open'),
                        'status': f['status'],
                        'due_at': f['due_at'],
                        'session_id': f['session_id'],
                    }
                )
            return result
