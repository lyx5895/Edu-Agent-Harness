"""Subject-neutral contracts. No course ids or subject allowlist."""
import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from .criteria import Criterion
from .pedagogy import TeachingContract, validate_requirements


class TaskDraft(TeachingContract):
    criteria: list[Criterion] = Field(default_factory=list, max_length=4)

    _requirements = field_validator("check_requirements")(validate_requirements)

    domain: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=240)
    question: str = Field(min_length=1, max_length=900)
    rubric: str = Field(min_length=1, max_length=600)
    reference: str = Field(min_length=1, max_length=700)
    source_quote: str = Field(default='', max_length=250)
    prerequisite: str = Field(default='', max_length=240)
    algebra_initial: str = Field(default='', max_length=160)
    algebra_kind: Literal['none', 'expression', 'equation'] = 'none'


class Assessment(BaseModel):
    status: Literal['correct', 'incorrect', 'incomplete', 'unknown']
    confidence: Literal['high', 'medium', 'low']
    # 1-based index into the server-numbered student_sentences; 0 = cannot locate.
    evidence_sentence: int = Field(default=0, ge=0, le=200)
    evidence_quote: str = Field(min_length=1, max_length=250)
    observation: str = Field(max_length=350)
    hypothesis: str = Field(max_length=250)


class ReplyMeaning(BaseModel):
    outcome: Literal['slip', 'misconception', 'not_helped', 'helped', 'unknown']
    hypothesis: str = Field(max_length=240)


class KnowledgeGuess(BaseModel):
    points: list[str] = Field(max_length=6)


class KnowledgeChoice(BaseModel):
    ids: list[str] = Field(max_length=4)


class Review(BaseModel):
    # Reason first so constrained decoding commits to a verdict after the analysis.
    reason: str = Field(max_length=250)
    acceptable: bool


POLICY_VERSION = 'open-task-policy-v4'
HINT_LEVELS = (
    ('scaffold_concept', '第一级提示：只点明关键概念或依据。'),
    ('scaffold_step', '第二级提示：说明下一步做什么及依据。'),
    ('scaffold_example', '第三级提示：给同构的半完成例子，留关键一步。'),
)


def choose_v1(o):
    if o['phase'] == 'check':
        return {'action': 'assess_only', 'reason': '独立检查先评估作答，不向学生提供答案或提示。'}
    if o['kind'] == 'check':
        return {'action': 'create_check', 'reason': '按当前目标生成未见检查任务，先审核再呈现。'}
    if o['kind'] == 'explain':
        return {'action': 'explain', 'reason': '学生明确请求讲解，之后的完成记为辅助完成。'}
    if o['kind'] == 'hint':
        return {'action': 'scaffold', 'reason': '根据当前困难和已用帮助给出下一步提示。'}
    if o['kind'] == 'reply':
        outcome = o.get('outcome', 'unknown')
        action = {'slip':'self_correct', 'helped':'self_correct',
                  'not_helped':'change_explanation', 'misconception':'prerequisite'}.get(outcome, 'clarify')
        return {'action': action, 'reason': '根据学生原文支持的暂时解释调整帮助，不将自述记为能力证据。'}
    if o['status'] == 'correct':
        return {'action': 'offer_check', 'reason': '当前任务完成，邀请进行新的无提示检查。'}
    if o['status'] == 'unknown':
        return {'action': 'clarify', 'reason': '证据不足或验证结果冲突，先澄清。'}
    if o.get('failures', 0) > 1:
        return {'action': 'change_explanation', 'reason': '重复失败，需要改变解释方式而非重复同一提示。'}
    return {'action': 'probe', 'reason': '询问作答依据，区分知识缺口与操作失误。'}


def language(text):
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    return 'zh' if cjk * 3 >= latin or cjk >= 20 else 'en'


def sentences(text):
    """Exact, non-empty spans of text split at line breaks and sentence ends."""
    spans = re.findall(r'[^\n。！？；!?]+(?:[。！？；!?]+|(?<=\S)\.(?=\s|$))?', text)
    out = []
    for span in spans:
        # Split at ". " but keep decimals such as 1.5 intact.
        out.extend(p.strip() for p in re.split(r'(?<=[^\d\s]\.)\s+|(?<=\d\.)\s+(?=[A-Z])', span))
    return [s[:250] for s in out if s][:200]


def public_task(task):
    return {**{k: task[k] for k in ('id', 'domain', 'objective', 'question', 'phase', 'source_ids')},
            'task_type': task.get('task_type', 'open'), 'source_pages': task.get('source_pages', []),
            'check_requirements': task.get('check_requirements', []),
            'criteria':[{'id':x['id'],'description':x['description']} for x in task.get('criteria',[])]}


def choose_v2(o):
    if o['phase']=='teach' and o.get('fallbacks',0)>=2 and o['kind'] not in ('check','explain') and o.get('status')!='correct':
        return {'action':'teacher_review','reason':'连续反馈未通过审核，需要教师介入而非重复无效提示。'}
    if o['phase']=='teach' and o['kind']=='attempt' and o.get('status') in ('incorrect','incomplete') and o.get('prior_support_needed'):
        return {'action':'prerequisite','reason':'同一课程目标已有困难证据，接续先修诊断，避免重新重复首次追问。'}
    return choose_v1(o)


def choose_v3(o):
    decision = choose_v2(o)
    if decision['action'] != 'scaffold':
        return decision
    # Escalate support only as hints accumulate on the same task.
    action, reason = HINT_LEVELS[min(o.get('hints', 0), len(HINT_LEVELS)-1)]
    return {'action': action, 'reason': reason}


def choose_v4(o):
    decision = choose_v3(o)
    if decision['action'] == 'probe' and o.get('grounded'):
        return {'action': 'locate', 'reason': '评分已逐字定位到学生原文中的问题，指出位置并追问，不给改正结果。'}
    return decision


STALL_ACTIONS = {'probe', 'locate', 'clarify', 'self_correct', 'change_explanation', 'prerequisite'}


def choose_v5(o):
    decision = choose_v4(o)
    if o['phase'] != 'teach' or decision['action'] not in STALL_ACTIONS:
        return decision
    stalls = o.get('stalls', 0)
    unhelped = o.get('outcome') in ('not_helped', 'unknown') if o['kind'] == 'reply' else o.get('status') in ('incorrect', 'incomplete')
    if stalls < 3 and not (stalls >= 2 and unhelped):
        return decision
    if o.get('hints', 0) >= len(HINT_LEVELS):
        return {'action': 'teacher_review', 'reason': '提示阶梯已用尽，学生仍未取得进展，转教师介入。'}
    action, reason = HINT_LEVELS[o.get('hints', 0)]
    return {'action': action, 'reason': f'连续{stalls}轮追问没有带来进展，按提示阶梯升级支持。' + reason}


POLICIES = {'open-task-policy-v5': choose_v5, 'open-task-policy-v4': choose_v4, 'open-task-policy-v3': choose_v3,
            'open-task-policy-v2': choose_v2, 'open-task-policy-v1': choose_v1}
choose = choose_v4
