"""Tutor systems under comparison. Raw systems see the same problem, gold solution
and dialogue as the harness; only the scaffolding around the model differs."""
import re
import tempfile
import uuid
from pathlib import Path

from .common import ROOT, chat
from .data import mathtutorbench_prompt

TUTOR_SYSTEM = ('You are a math tutor helping a student who made a mistake. Help the student find and fix the '
                'mistake themselves. Do not reveal the final answer. Reply in the language the student uses, '
                'in at most three sentences.')


class RawTutor:
    def __init__(self, endpoint, with_reference=False):
        self.endpoint = endpoint
        self.with_reference = with_reference
        self.name = f'raw-{endpoint}' + ('-ref' if with_reference else '')

    def judge_correctness(self, item):
        prompt = mathtutorbench_prompt('student_solution_correctness.yaml', **item)
        if self.with_reference:
            prompt = prompt.replace('Conversation:', 'Reference solution (correct): ' + item['reference_solution'] + '\nConversation:', 1)
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
    def __init__(self, policy=None):
        from harness.open_engine import OpenEngine
        from harness.open_model import OpenModel
        self.OpenEngine, self.OpenModel = OpenEngine, OpenModel
        self.policy = policy
        self.name = 'harness-omniedu' + ('-' + policy.rsplit('-', 1)[-1] if policy else '')
        self.tmp = tempfile.TemporaryDirectory(prefix='edu-bench-')

    def engine(self):
        model = self.OpenModel()
        model.logs = ROOT / 'logs/bench-model-calls.jsonl'
        return self.OpenEngine(Path(self.tmp.name) / f'{uuid.uuid4().hex}.sqlite', model, policy=self.policy)

    def start(self, engine, question, reference):
        assignment = engine.publish_assignment({
            'class_id': 'benchmark', 'course_id': 'math', 'concept_id': 'word-problem', 'domain': 'Math',
            'objective': 'Solve the word problem correctly with sound reasoning', 'question': question,
            'rubric': 'The final answer must match the reference solution and every reasoning and calculation step must be correct.',
            'reference': reference,
            'task_type': 'problem_solving', 'reference_origin': 'teacher_authored'})
        return engine.create('bench-' + uuid.uuid4().hex[:8], assignment_id=assignment['assignment_id'])

    def judge_correctness(self, item):
        engine = self.engine()
        session = self.start(engine, item['question'], item['reference_solution'])
        result = engine.operation(session['session_id'], 'attempt', 'attempt', {'text': item['student_chat_solution'][:1600]})
        status = result['status']
        return {'status': status, 'flagged_error': status != 'correct', 'abstained': status == 'unknown',
                'feedback': result['feedback'], 'action': result.get('action')}

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
        return {'tutor': result['feedback'], 'action': result.get('action'), 'status': result.get('status'), 'operation': kind}
