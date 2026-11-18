"""Scripted model for edu_core tests: each task maps to a handler(role, user_dict) -> reply dict."""

import json


class FakeLLM:
    def __init__(self, handlers, models=None):
        self.handlers = handlers
        self.calls = []
        self.models = models or {
            'generator': 'fake-gen',
            'grader': 'fake-grader',
            'analyst': 'fake-analyst',
            'reviewer': 'fake-reviewer',
        }

    def model_for(self, role):
        return self.models[role]

    def complete(self, role, task, system, user, schema):
        payload = json.loads(user)
        self.calls.append({'role': role, 'task': task, 'user': payload, 'system': system})
        handler = self.handlers.get(task) or self.handlers.get(f'{role}:{task}')
        if handler is None:
            raise AssertionError(f'unexpected model call: {role} {task}')
        reply = handler(role, payload)
        result = schema.model_validate(reply)
        meta = {
            'task': task,
            'role': role,
            'model_requested': self.models[role],
            'model_returned': self.models[role] + '-0001',
            'usage': {'total_tokens': 10},
            'latency_ms': 1,
            'request': [{'role': 'user', 'content': user}],
            'response': json.dumps(reply, ensure_ascii=False),
        }
        return result, meta

    def tasks(self):
        return [c['task'] for c in self.calls]


def judge_points(rules, *, confidence='high'):
    """Grade handler: rules = {point_id: (needle, tag)}; earned with the needle quoted if present."""

    def handler(role, payload):
        answer = payload['学生作答']
        out = []
        for p in payload['评分要点']:
            needle, tag = rules[p['point_id']]
            if needle in answer:
                out.append(point(p['point_id'], p['满分'], 'earned', needle, confidence=confidence))
            else:
                out.append(point(p['point_id'], 0, 'missed', '', tag=tag, confidence=confidence))
        return {'points': out}

    return handler


def point(pid, score, status, quote, *, tag='', note='', confidence='high', reason='依据评分要点判定'):
    return {
        'point_id': pid,
        'score': score,
        'status': status,
        'evidence_quote': quote,
        'reason': reason,
        'error_tag': tag,
        'error_note': note,
        'confidence': confidence,
    }
