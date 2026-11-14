"""MRBench-style judge (Qwen3.5-27B). Labels and definitions follow the MRBench
human annotation guideline; agreement with human labels is measured before use."""

from collections import Counter

from .common import chat_json

THREE = ['Yes', 'To some extent', 'No']
DIMENSIONS = {
    'Mistake_Identification': THREE,
    'Mistake_Location': THREE,
    'Revealing_of_the_Answer': ['Yes (and the answer is correct)', 'Yes (but the answer is incorrect)', 'No'],
    'Providing_Guidance': THREE,
    'Actionability': THREE,
    'Coherence': THREE,
    'Tutor_Tone': ['Encouraging', 'Neutral', 'Offensive'],
    'humanlikeness': THREE,
}

GUIDE = """You are an expert annotator applying the MRBench evaluation taxonomy to a tutor's next response in a math tutoring dialogue. The student has made a mistake. Judge each dimension independently.

- Mistake_Identification: Has the tutor identified a mistake in the student's response? Yes = clearly signals a mistake (including by stating the correct value); To some extent = hints something may be wrong but sounds uncertain (e.g. "Are you sure?"); No = does not identify it (e.g. praises a wrong answer).
- Mistake_Location: Does the response accurately point to a genuine mistake and where it is? Yes = points to the exact erroneous step/value; To some extent = shows some awareness, e.g. guidance toward the right area; No = no detail about the mistake.
- Revealing_of_the_Answer: Does the tutor reveal the FINAL answer of the problem? Intermediate values given as hints do not count. Use the gold solution to decide whether a revealed final answer is correct.
- Providing_Guidance: Does the tutor offer correct and relevant guidance (explanation, hint, example, guiding question)? Yes = correct and relevant to the mistake; To some extent = guidance is partly incorrect or incomplete; No = none.
- Actionability: Is it clear what the student should do next? A response that simply reveals the final answer is not actionable.
- Coherence: Is the response logically consistent with the student's previous response and the dialogue (no contradiction with the task or earlier turns)?
- Tutor_Tone: Encouraging (support, care, "good try", "let's work together"), Neutral, or Offensive (toxic, or curt/frustrated and discouraging).
- humanlikeness: Could a human tutor plausibly have written it? No for formulaic one-liners, leaked prompt instructions, or context-free boilerplate. Responses in a different language than the dialogue are judged on content, not language.

First write brief reasoning, then the labels."""


def judge_schema():
    props = {'reasoning': {'type': 'string', 'maxLength': 800}}
    props.update({name: {'type': 'string', 'enum': labels} for name, labels in DIMENSIONS.items()})
    return {'type': 'object', 'properties': props, 'required': list(props), 'additionalProperties': False}


def judge(conversation_history, gold_solution, response):
    content = (
        f'Conversation history:\n{conversation_history}\n\nGold solution (for reference only):\n{gold_solution}'
        f'\n\nTutor response to evaluate:\n{response}'
    )
    return chat_json(
        'judge',
        [{'role': 'system', 'content': GUIDE}, {'role': 'user', 'content': content}],
        'mrbench_judgment',
        judge_schema(),
        max_tokens=1200,
    )


def lenient(dimension, label):
    """BEA 2025 lenient setting: merge 'To some extent' with 'Yes' for the 3-way dims,
    and the two 'Yes' variants for answer revealing."""
    if label is None:
        return None
    if dimension == 'Revealing_of_the_Answer':
        return 'Yes' if label.startswith('Yes') else 'No'
    if dimension == 'Tutor_Tone':
        return 'Offensive' if label == 'Offensive' else 'OK'
    return 'No' if label == 'No' else 'Yes'


def kappa(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / n / n
    return None if expected == 1 else (observed - expected) / (1 - expected)


def agreement(records):
    summary = {}
    for dim in DIMENSIONS:
        exact = [(r['human'].get(dim), r['judge'].get(dim)) for r in records if 'judge' in r]
        loose = [(lenient(dim, a), lenient(dim, b)) for a, b in exact]
        valid = [(a, b) for a, b in exact if a is not None]
        summary[dim] = {
            'n': len(valid),
            'exact_accuracy': round(sum(a == b for a, b in valid) / len(valid), 3) if valid else None,
            'lenient_accuracy': round(sum(a == b for a, b in loose if a is not None) / len(valid), 3)
            if valid
            else None,
            'lenient_kappa': round(kappa(loose), 3) if kappa(loose) is not None else None,
            'human_positive_rate': round(
                sum(a == ('Yes' if dim != 'Tutor_Tone' else 'OK') for a, _ in loose if a) / len(valid), 3
            )
            if valid
            else None,
        }
    return summary
