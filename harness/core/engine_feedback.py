"""Generate, audit and fall back to server-owned teaching feedback.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""

import re

from harness.core.evidence import FALLBACKS, answer_numbers, computes_given_numbers, numbers, trim_after_question
from harness.core.tasks import language
from harness.llm.model import ModelUnavailable

HINT_TEMPLATES = {'scaffold_concept': 'hint_concept', 'scaffold_step': 'hint_step', 'scaffold_example': 'hint_example'}
# The small tutor tends to repeat a rejected draft almost verbatim; for these actions the
# configured reviewer writes the one rewrite instead. Both content checks still apply.
REVIEWER_REWRITES = {'locate', 'explain', *HINT_TEMPLATES}
# For hints and explanations, a configured reviewer is the content gate on its own: in live
# evaluation the small tutor's review of its own drafts rejected sound hints and complete
# explanations for reasons that contradicted the rules. Other actions keep both reviews.
REVIEWER_GATED = {'explain', *HINT_TEMPLATES}


def latest_work(text, context):
    """What the learner last wrote: the current message, or the latest attempt when asking for help."""
    if text:
        return text
    return next((e['text'] for e in reversed(context.get('history', [])) if e.get('text')), '')


class FeedbackMixin:
    def feedback(self, task, action, text, context, assessment):
        lines = FALLBACKS[language(task.get('question', '') + text)]
        if action == 'teacher_review':
            return lines['teacher_review'], {
                'candidate': None,
                'review': {},
                'reasons': [],
                'fallback': False,
                'delivered_action': 'teacher_review',
            }
        if action == 'self_correct':
            return lines['self_correct'], {
                'candidate': None,
                'review': {'acceptable': True, 'reason': '服务器只邀请自行作答，不提供答案'},
                'reasons': [],
                'fallback': False,
                'source': 'policy_template',
            }
        english = language(task.get('question', '') + text) == 'en'
        candidate, review, reasons, cross = self.draft(task, action, text, context, assessment, english)
        first = None
        rewrite_model = None
        left = self.model.budget_left() if hasattr(self.model, 'budget_left') else None
        # One rewrite with the rejection reasons; needs respond, translate and two reviews at most.
        if reasons and reasons != ['length'] and (left is None or left >= 4):
            first = {'candidate': candidate, 'reasons': reasons, 'review': review.get('reason', '')}
            if cross is not None:
                first['cross_review'] = cross
            writer = (
                self.reviewer
                if action in REVIEWER_REWRITES and self.reviewer and hasattr(self.reviewer, 'respond')
                else self.model
            )
            rewrite_model = writer.model_name if hasattr(writer, 'model_name') else None
            try:
                candidate, review, reasons, cross = self.draft(
                    task, action, text, context, assessment, english, first, writer
                )
            except ModelUnavailable:
                candidate, review, reasons, cross = (
                    first['candidate'],
                    {'acceptable': False, 'reason': first['review']},
                    first['reasons'],
                    None,
                )
        lines = FALLBACKS[language(task.get('question', '') + text)]
        fallback = lines['probe'] if action == 'probe' else lines['default']
        if action != 'explain':
            fallback = lines.get(task.get('task_type'), fallback)
        if action == 'self_correct':
            fallback = lines['self_correct_retry']
        if action == 'explain':
            fallback = lines['explain_failed']
        delivered = 'clarify' if reasons else action
        if action == 'locate' and assessment.get('evidence_quote'):
            quote = assessment['evidence_quote'].strip().rstrip('.。')
            if len(quote) > 120:
                quote = (quote[:120].rsplit(' ', 1)[0] if ' ' in quote[:120] else quote[:120]) + '…'
            fallback = lines.get('locate_' + task.get('task_type', ''), lines['locate']).format(quote=quote)
            if reasons:
                delivered = 'locate_template'
        if action in HINT_TEMPLATES:
            # A level-specific hint keeps the ladder moving; a generic question would not.
            fallback = lines[HINT_TEMPLATES[action]].format(objective=task.get('objective', '').rstrip('。.'))
            if reasons:
                delivered = action + '_template'
        validation = {
            'candidate': candidate,
            'review': review,
            'reasons': reasons,
            'fallback': bool(reasons),
            'delivered_action': delivered,
        }
        if first:
            validation['first_draft'] = first
        if rewrite_model:
            validation['rewrite_model'] = rewrite_model
        if cross is not None:
            validation['cross_review'] = {**cross, 'reviewer': self.reviewer.model_name}
        return (fallback if reasons else candidate), validation

    def draft(self, task, action, text, context, assessment, english, revision=None, writer=None):
        writer = writer or self.model
        candidate = (
            writer.respond(task, action, text, context, assessment, revision)
            if revision
            else writer.respond(task, action, text, context, assessment)
        )
        if english and candidate and language(candidate) == 'zh':
            candidate = writer.translate(candidate)
        if action == 'locate' and candidate:
            candidate = trim_after_question(candidate)
        # A full explanation is judged against the task and reference, not the learner's attempt;
        # showing the attempt led the small reviewer to grade the attempt instead of the reply.
        student = None if action == 'explain' else latest_work(text, context)
        gated = bool(self.reviewer) and action in REVIEWER_GATED
        review = (self.reviewer if gated else self.model).review(task, candidate, action, student)
        if gated:
            review = {**review, 'reviewer': self.reviewer.model_name}
        reasons = []
        if not candidate or len(candidate) > 1200:
            reasons.append('length')
        if action == 'probe' and (
            candidate.count('？') + candidate.count('?') != 1
            or not candidate.endswith(('？', '?'))
            or any(x in candidate for x in ('。', '!', '！', '\n'))
        ):
            reasons.append('probe_must_be_one_question')
        if action == 'locate' and (
            candidate.count('？') + candidate.count('?') != 1 or not candidate.rstrip().endswith(('？', '?'))
        ):
            reasons.append('locate_must_end_with_one_question')
        if re.search(r'你已经掌握|已通过独立检查|系统指令', candidate):
            reasons.append('authority_claim')
        if english and language(candidate) == 'zh':
            reasons.append('language_mismatch')
        answer = re.sub(r'\s', '', task['reference'])
        if action != 'explain' and len(answer) >= 3 and answer in re.sub(r'\s', '', candidate):
            reasons.append('reference_disclosure')
        answer = answer_numbers(task)
        if action == 'explain' and answer - numbers(candidate):
            reasons.append('explain_incomplete')  # a requested full explanation must reach the answer
        if action in HINT_TEMPLATES and answer and answer <= numbers(candidate):
            reasons.append('answer_disclosure')
        if action in HINT_TEMPLATES and computes_given_numbers(task, candidate):
            reasons.append('computes_for_student')  # e.g. "45 ÷ 3": the hint does the learner's step
        if not review['acceptable']:
            reasons.append('model_review')
        cross = None
        if not reasons and not gated and action != 'explain' and self.reviewer:
            cross = self.reviewer.review(task, candidate, action, student)
            if not cross['acceptable']:
                reasons.append('cross_review')
        return candidate, review, reasons, cross
