"""Simulated student seeded with a real erroneous solution and its annotated error.
Prompted simulators are known to be imperfect (see sim-student-eval); results
compare tutors under the same simulator and are not learning-outcome evidence."""
from .common import chat_json, final_number, mentions_number

PERSONA = """You are role-playing a middle-school student in a tutoring chat. Stay in character.

Problem: {question}
Your earlier solution (it contains a mistake you are NOT aware of): {solution}
Hidden misconception behind it (never mention it explicitly): {error}

Rules:
- Reply briefly (1-3 sentences) like a real student, answering what the tutor asked.
- Keep believing your approach until the tutor's message actually helps you see the problem; do not suddenly become perfect.
- If the tutor's help makes you realise the mistake, redo the work yourself and submit a new final answer.
- Never claim to understand without showing work. You do not know the correct answer in advance.
- Set submits_answer=true only when your message contains a complete (re)solution with a final numeric answer, and put that number in final_answer; otherwise false and empty."""

SCHEMA = {'type': 'object', 'properties': {
    'message': {'type': 'string', 'maxLength': 500},
    'submits_answer': {'type': 'boolean'},
    'final_answer': {'type': 'string', 'maxLength': 40}},
    'required': ['message', 'submits_answer', 'final_answer'], 'additionalProperties': False}


def student_reply(problem, transcript):
    system = PERSONA.format(question=problem['question'], solution=problem['student_chat_solution'], error=problem['error_description'])
    messages = [{'role': 'system', 'content': system}]
    for turn in transcript:
        messages.append({'role': 'user' if turn['speaker'] == 'tutor' else 'assistant', 'content': turn['text']})
    return chat_json('judge', messages, 'student', SCHEMA, max_tokens=1000, temperature=0.3)


def submitted_gold(turn, gold):
    # The simulator sometimes fills final_answer with words or junk; fall back to the last number it wrote.
    return bool(turn.get('submits_answer')) and gold in (final_number(turn.get('final_answer')), final_number(turn.get('text')))


def run_dialogue(tutor, problem, max_turns=6):
    gold = problem['gold_answer']
    session = tutor.session(problem)
    transcript = [{'speaker': 'student', 'text': problem['student_chat_solution'], 'submits_answer': True,
                   'final_answer': final_number(problem['student_chat_solution'])}]
    outcome = {'solved': False, 'turns': 0, 'leaked_turn': None, 'student_stated_gold': False}
    for turn in range(1, max_turns + 1):
        last = transcript[-1]
        tutor_out = session.turn(last['text'], last['submits_answer'])
        text = tutor_out.get('tutor', '')
        if tutor_out.get('action') == 'completed':
            break
        transcript.append({'speaker': 'tutor', 'text': text, **{k: v for k, v in tutor_out.items() if k != 'tutor'}})
        outcome['turns'] = turn
        stated = any(t['speaker'] == 'student' and mentions_number(t['text'], gold) for t in transcript)
        if outcome['leaked_turn'] is None and not stated and mentions_number(text, gold) and not mentions_number(problem['question'], gold):
            outcome['leaked_turn'] = turn
        if tutor_out.get('action') == 'teacher_review':
            outcome['escalated'] = True
            break
        reply = student_reply(problem, transcript)
        transcript.append({'speaker': 'student', 'text': reply['message'], 'submits_answer': reply['submits_answer'],
                           'final_answer': reply['final_answer']})
        if submitted_gold(transcript[-1], gold):
            outcome['solved'] = True
            outcome['solved_after_leak'] = outcome['leaked_turn'] is not None
            # Let the tutor acknowledge; harness records evidence on this attempt.
            closing = session.turn(reply['message'], True)
            transcript.append({'speaker': 'tutor', 'text': closing.get('tutor', ''), **{k: v for k, v in closing.items() if k != 'tutor'}})
            break
    outcome['transcript'] = transcript
    return outcome
