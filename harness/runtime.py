"""Bounded context, teaching execution, output checks and auditable traces."""
import re
from copy import deepcopy
from .course import SKILLS
from .policy import VERSION, decide

PROBES = [
    '括号外的数应该乘括号里的哪些项？请说说你的理由。',
    '括号外的负号对括号内各项的符号有什么影响？',
    '你根据什么判断哪些项可以合并？',
    '分母不同的系数相加前，需要先做什么？',
    '改变等式一边时，另一边需要怎样处理？',
    '为了保持等式成立，两边的乘除操作应满足什么条件？',
    '你打算先消去哪一项，为什么这样能保持等式成立？',
    '去括号时，你是怎样处理括号里的每一项的？',
    '把未知数项放在同一边时，两边应做什么相同的操作？',
    '去分母时，相乘的对象是某一项还是等式两边的整体？',
]


def context(events, item):
    """Only relevant bounded evidence; never copy hidden answers into dialogue."""
    recent = []
    for e in events[-6:]:
        p = e['payload']
        recent.append({'event_id': e['id'], 'kind': e['type'],
                       **{k: str(p[k])[:160] for k in ('text', 'feedback', 'hint', 'status') if k in p}})
    return {'skill': item['skill'], 'source': item['source'], 'recent_evidence': recent,
            'budget': {'max_events': 6, 'max_chars_per_field': 160}}


def fallback(action, item):
    probe = PROBES[int(item['skill'][1:]) - 1]
    if action == 'probe':
        return probe
    if action == 'clarify':
        return '请指出你不确定的那一步，并说说你当时用了什么规则？'
    if action == 'self_correct':
        return '请按你刚才说明的规则重新写出这一步，再提交作答。'
    if action in ('concept_hint', 'prerequisite_review'):
        return SKILLS[item['skill']]['rule'] + ' ' + probe
    if action == 'step_hint':
        return item['hints'][1]
    return item['hints'][2]


def guard(candidate, action, item):
    """Fail closed for generative conceptual text; mathematical steps are authored.
    This conservative lexical guard is not a semantic leakage proof.
    """
    reasons = []
    if not candidate or len(candidate) > 300:
        reasons.append('length')
    if re.search(r'[0-9０-９a-zA-Z=＝+*/<>×÷\\$]', candidate):
        reasons.append('unverified_math')
    if re.search(r'答案[是为：:]|结果[是为：:]|等于|已经掌握|通过检查', candidate):
        reasons.append('answer_or_mastery_claim')
    if candidate.count('？') + candidate.count('?') > 1:
        reasons.append('multiple_questions')
    if action in ('probe', 'clarify') and not ('？' in candidate or '?' in candidate):
        reasons.append('missing_question')
    return {'accepted': not reasons, 'reasons': reasons,
            'scope': 'conservative_lexical_guard', 'replacement': bool(reasons)}


def execute(model, item, observation, learner_state, ctx, diagnosis=None):
    decision = decide(observation)
    action = decision['action']
    packet = {'question': item['prompt'], 'student_context': ctx,
              'learner_state': deepcopy(learner_state),
              'action': action, 'reason': decision['reason'],
              'rule': SKILLS[item['skill']]['rule'],
              'diagnostic_hypothesis': (diagnosis or {}).get('hypothesis', '')[:160],
              'first_error': (diagnosis or {}).get('first_error')}
    if action in ('step_hint', 'worked_example'):
        candidate = fallback(action, item)
        validation = {'accepted': True, 'reasons': [], 'scope': 'authored_course', 'replacement': False}
    else:
        candidate = model.teach(packet)
        validation = guard(candidate, action, item)
    output = fallback(action, item) if validation['replacement'] else candidate
    step = (diagnosis or {}).get('first_error')
    # Location comes from the verifier when available, never generated arithmetic.
    verified_step = ctx.get('verification', {}).get('first_error')
    step = verified_step or step
    if action == 'probe' and type(step) is int and 1 <= step <= 20:
        output = f'先检查第{step}步。' + output
    return output, {'policy_version': VERSION, 'input': observation, 'decision': decision,
                    'context': packet, 'candidate': candidate, 'validation': validation,
                    'output': output, 'wait_for_student': True,
                    'tools': ['course.retrieve'], 'generation_calls_max': 2}


def replay(traces):
    checks = []
    for trace in traces:
        supported = trace['policy_version'] == VERSION
        actual = decide(trace['input']) if supported else None
        checks.append({'event_id': trace['event_id'], 'supported': supported,
                       'matches': supported and actual == trace['decision'], 'computed': actual})
    return {'policy_version': VERSION, 'trace_count': len(checks),
            'scope': 'policy decisions only; model generations are recorded, not regenerated',
            'kind': 'deterministic_policy_replay', 'model_called': False,
            'passed': all(c['matches'] for c in checks), 'turns': checks}
