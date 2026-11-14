"""System instructions OmniEdu was fine-tuned with (dataset lhpku20010120/Omni-Edu,
system_prompt_registry.json), and which harness call uses which one.

In the 'omniedu' prompt style the registry text is the whole system message, as in
training; the harness rules move to the start of the user message. The 'mixed' style does
this only for judging and tagging calls, where it measurably helped; tutoring replies keep
the harness system prompt."""

REGISTRY = {
    'answer_assessment': "Assess the learner's answer accurately. State what is correct, identify the first important error or omission, and give focused feedback or the next correction without unnecessary re-solving.",
    'writing_feedback': "Evaluate the student's writing against the supplied task and rubric. Give specific, evidence-based feedback that identifies strengths and the most useful improvements without inventing unsupported details.",
    'active_probe': 'Facilitate active learning with one actionable question that asks the learner to calculate, compare, retrieve, test, explain, or inspect something relevant. Do not reveal the final answer.',
    'oatutor_guidance': 'Guide the learner with a targeted next step based on the current problem and work. Prefer a concise hint or question that makes the learner reason instead of giving away the final answer.',
    'direct_explanation': "Give a clear, accurate explanation adapted to the learner's request. Cover the essential idea and reasoning directly, using a useful example or representation when appropriate.",
    'knowledge_point': 'This is a curriculum and knowledge-point task. Identify or judge the relevant concepts, standards, prerequisites, or relations from the provided information, including images when present, and follow the requested output format exactly.',
    'general_assistant': "You are a helpful, accurate, and concise assistant. Follow the user's task, language, format, tone, and length requirements, and use all supplied context.",
}

RESPOND = {
    'probe': 'active_probe',
    'clarify': 'active_probe',
    'locate': 'oatutor_guidance',
    'scaffold': 'oatutor_guidance',
    'scaffold_concept': 'oatutor_guidance',
    'scaffold_step': 'oatutor_guidance',
    'scaffold_example': 'oatutor_guidance',
    'change_explanation': 'oatutor_guidance',
    'prerequisite': 'direct_explanation',
    'explain': 'direct_explanation',
}


MIXED = {'answer_assessment', 'writing_feedback', 'knowledge_point'}


def assessment_role(task):
    return 'writing_feedback' if task.get('task_type') == 'writing' else 'answer_assessment'


def messages(style, role, rules, content):
    """Chat messages for one call. content is a string or an OpenAI content-part list."""
    if role is None or not (style == 'omniedu' or (style == 'mixed' and role in MIXED)):
        return [{'role': 'system', 'content': rules}, {'role': 'user', 'content': content}]
    header = '要求：\n' + rules + '\n\n输入：\n'
    user = header + content if isinstance(content, str) else [{'type': 'text', 'text': header}, *content]
    return [{'role': 'system', 'content': REGISTRY[role]}, {'role': 'user', 'content': user}]
