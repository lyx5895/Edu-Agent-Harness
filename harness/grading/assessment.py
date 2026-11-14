"""Combine scoped tool evidence with the complete task rubric."""

import contextlib
import re


def adjudicate(task, text, verification, assessment):
    model_status = assessment['status'] if assessment['confidence'] == 'high' else 'unknown'
    tool_status = verification['status']
    full = verification.get('scope') == 'whole_task'
    if full and tool_status != 'unknown':
        return tool_status, verification['source']
    if tool_status == 'incorrect':
        return 'incorrect', 'scoped_tool'
    # An algebra result never proves explanation, units, or requested form.
    status = model_status
    question = task['question'].lower()
    if (
        re.search(r'解释|说明|证明|理由|依据|explain|justify|prove', question)
        and status != 'incorrect'
        and re.fullmatch(r'[\dx\s=+*/().^;；→\-]+', text)
    ):
        status = 'incomplete'
    if re.search(r'因式分解|factor', question) and '(' not in text:
        equivalent = tool_status == 'correct'
        if not equivalent and task.get('reference'):
            from harness.grading.algebra import expression

            with contextlib.suppress(ValueError, TypeError, SyntaxError):
                equivalent = expression(text) == expression(task['reference'])
        if equivalent:
            status = 'incomplete'
    # A quotation proves source presence, not the additional explanation requested.
    quotes = [c.get('expected', '') for c in task.get('criteria', []) if c.get('kind') == 'source_quote']
    requirements = ' '.join([task.get('question', ''), task.get('rubric', ''), *task.get('check_requirements', [])])
    explain = re.search(
        r'(?:并|还需|同时|还要).{0,12}(?:解释|说明|分析|论证)|and.{0,20}(?:explain|justify|analy[sz]e)',
        requirements,
        re.I,
    )

    def normalize(value):
        return re.sub(r'[\W_]+', '', value).lower()

    remaining = normalize(text)
    if explain and quotes:
        for quote in quotes:
            normalized = normalize(quote)
            if normalized:
                remaining = remaining.replace(normalized, '')
        if not remaining and status != 'incorrect':
            status = 'incomplete'
    return status, 'model_rubric'
