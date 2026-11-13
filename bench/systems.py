"""Tutor systems under comparison. Raw systems see the same problem, gold solution
and dialogue as the harness; only the scaffolding around the model differs."""
import json
import re
import tempfile
import uuid
from pathlib import Path

from .common import ENDPOINTS, ROOT, chat
from .data import mathtutorbench_prompt

TUTOR_SYSTEM = ('You are a math tutor helping a student who made a mistake. Help the student find and fix the '
                'mistake themselves. Do not reveal the final answer. Reply in the language the student uses, '
                'in at most three sentences.')


TASK_TYPES = {'数学': 'problem_solving', '物理': 'problem_solving', '化学': 'problem_solving',
              '生物': 'concept', '语文': 'reading', '英语': 'concept'}


class RawTutor:
    def __init__(self, endpoint, with_reference=False):
        self.endpoint = endpoint
        self.with_reference = with_reference
        self.name = f'raw-{endpoint}' + ('-ref' if with_reference else '')

    def judge_correctness(self, item):
        prompt = mathtutorbench_prompt('student_solution_correctness.yaml', **item)
        if item.get('subject'):
            prompt = prompt.replace('experienced math teacher', 'experienced teacher').replace('Problem:', 'Question:')
        if self.with_reference:
            extra = 'Reference solution (correct): ' + item['reference_solution']
            if item.get('rubric'):
                extra += '\nGrading rubric: ' + item['rubric']
            prompt = prompt.replace('Conversation:', extra + '\nConversation:', 1)
        reply = chat(self.endpoint, [{'role': 'user', 'content': prompt}], max_tokens=8)
        text = reply.strip().lower()
        flagged = 'yes' in text or 'no' not in text
        return {'flagged_error': flagged, 'status': 'incorrect' if flagged else 'correct', 'raw': reply}

    def locate(self, item):
        reply = chat(self.endpoint, [{'role': 'user', 'content': mathtutorbench_prompt('mistake_location.yaml', **item)}], max_tokens=8)
        digits = re.findall(r'\d+', reply)
        return {'step': int(digits[0]) if digits else None, 'raw': reply}

    def respond(self, conversation):
        prompt = (TUTOR_SYSTEM + '\n\nProblem: ' + conversation['question'] + '\nGold solution (never reveal it): '
                  + conversation['Ground_Truth_Solution'] + '\n\nConversation:\n' + conversation['conversation_history'] + '\nTutor:')
        return {'response': chat(self.endpoint, [{'role': 'user', 'content': prompt}], max_tokens=200).strip()}

    def session(self, problem):
        return RawSession(self.endpoint, problem)


class RawSession:
    def __init__(self, endpoint, problem):
        self.endpoint = endpoint
        self.messages = [{'role': 'system', 'content': TUTOR_SYSTEM + '\n\nProblem: ' + problem['question']
                          + '\nGold solution (never reveal it): ' + problem['reference_solution']}]
        self.events = []

    def turn(self, student_message, submits_answer):
        self.messages.append({'role': 'user', 'content': student_message})
        reply = chat(self.endpoint, self.messages, max_tokens=200).strip()
        self.messages.append({'role': 'assistant', 'content': reply})
        return {'tutor': reply}


class HarnessTutor:
    def __init__(self, policy=None, cross_review=False):
        from harness.open_engine import OpenEngine
        from harness.open_model import OpenModel
        self.OpenEngine, self.OpenModel = OpenEngine, OpenModel
        self.policy, self.cross_review = policy, cross_review
        self.name = 'harness-omniedu' + ('-' + policy.rsplit('-', 1)[-1] if policy else '') + ('-xr' if cross_review else '')
        self.tmp = tempfile.TemporaryDirectory(prefix='edu-bench-')

    def engine(self):
        model = self.OpenModel()
        model.logs = ROOT / 'logs/bench-model-calls.jsonl'
        reviewer = None
        if self.cross_review:
            reviewer = self.OpenModel(*ENDPOINTS['judge'])
            reviewer.logs = model.logs
        return self.OpenEngine(Path(self.tmp.name) / f'{uuid.uuid4().hex}.sqlite', model, policy=self.policy, reviewer=reviewer)

    def start(self, engine, question, reference, item=None):
        item = item or {}
        if item.get('language') == 'zh':
            subject = item.get('subject', '数学')
            fields = {'domain': subject, 'objective': '正确回答题目并说明依据' if item.get('subject') else '正确求解应用题并给出推理',
                      'rubric': item.get('rubric') or '最终答案须与参考解答一致，且每一步推理和计算正确。',
                      'task_type': TASK_TYPES.get(subject, 'problem_solving')}
        else:
            fields = {'domain': 'Math', 'objective': 'Solve the word problem correctly with sound reasoning',
                      'rubric': 'The final answer must match the reference solution and every reasoning and calculation step must be correct.',
                      'task_type': 'problem_solving'}
        assignment = engine.publish_assignment({
            'class_id': 'benchmark', 'course_id': fields['domain'], 'concept_id': 'benchmark-item', 'question': question,
            'reference': reference, 'reference_origin': 'teacher_authored', **fields})
        return engine.create('bench-' + uuid.uuid4().hex[:8], assignment_id=assignment['assignment_id'])

    def judge_correctness(self, item):
        engine = self.engine()
        session = self.start(engine, item['question'], item['reference_solution'], item)
        result = engine.operation(session['session_id'], 'attempt', 'attempt', {'text': item['student_chat_solution'][:1600]})
        status = result['status']
        out = {'status': status, 'flagged_error': status != 'correct', 'abstained': status == 'unknown',
               'feedback': result['feedback'], 'action': result.get('action')}
        if self.cross_review:
            with engine.connect() as c:
                payload = json.loads(c.execute('SELECT payload FROM learning_events WHERE id=?', (result['event_id'],)).fetchone()[0])
            out['cross_review'] = payload.get('cross_review')
        return out

    def respond(self, conversation):
        engine = self.engine()
        session = self.start(engine, conversation['question'], conversation['Ground_Truth_Solution'])
        work = '\n'.join(t.removeprefix('Student:').strip() for t in conversation['student_turns'])[:1600]
        result = engine.operation(session['session_id'], 'attempt', 'attempt', {'text': work})
        return {'response': result['feedback'], 'status': result['status'], 'action': result.get('action')}

    def session(self, problem):
        return HarnessSession(self, problem)


class HarnessSession:
    def __init__(self, tutor, problem):
        self.engine = tutor.engine()
        self.session = tutor.start(self.engine, problem['question'], problem['reference_solution'])
        self.events = []

    def turn(self, student_message, submits_answer):
        sid = self.session['session_id']
        state = self.engine.session(sid)
        if state['completed']:
            return {'tutor': '', 'action': 'completed'}
        kind = 'reply' if state['pending_question'] and not submits_answer else 'attempt'
        result = self.engine.operation(sid, uuid.uuid4().hex, kind, {'text': student_message[:1600]}, state['version'])
        self.session = result['session']
        trace = next((t for t in self.engine.traces(sid) if t['event_id'] == result.get('trace_id')), {})
        validation = trace.get('output_validation', {})
        return {'tutor': result['feedback'], 'action': result.get('action'), 'status': result.get('status'), 'operation': kind,
                'delivered_action': validation.get('delivered_action'), 'fallback_reasons': validation.get('reasons', [])}
