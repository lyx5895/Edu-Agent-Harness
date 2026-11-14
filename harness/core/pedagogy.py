"""Task profiles describe teaching behavior, not an allowlist of subjects.
Applications can register additional profiles at startup; unknown profiles use open.
"""
from pydantic import BaseModel, Field


class TeachingContract(BaseModel):
    task_type: str = Field(default='open', min_length=1, max_length=60, pattern=r'^[a-z][a-z0-9_-]*$')
    check_requirements: list[str] = Field(default_factory=list, max_length=8)


PROFILES = {
    'open': {'label': '开放任务', 'strategy': '根据任务要求定位具体困难，选择一个可执行的下一步。'},
    'problem_solving': {'label': '解题与推理', 'strategy': '定位第一处不成立的步骤，区分概念错误、计算错误和表达遗漏；必要时给半完成例题。'},
    'concept': {'label': '概念理解', 'strategy': '用正反例或预测问题检验概念边界，再检查学生能否解释理由。'},
    'reading': {'label': '阅读与证据', 'strategy': '区分原文信息、推断和观点，引导学生定位原文证据并说明证据如何支持结论。'},
    'writing': {'label': '写作与表达', 'strategy': '围绕最重要的一项写作要求给局部修改建议，让学生保留自己的观点和表达；不代写全文。'},
    'experiment': {'label': '实验与数据分析', 'strategy': '检查变量、对照、数据、单位和结论的关系；区分观察结果与因果推断。'},
    'programming': {'label': '编程与算法', 'strategy': '通过输入输出例子、边界条件和反例定位问题，要求解释算法，不直接代写完整程序。'},
}


def register_profile(key, label, strategy):
    TeachingContract(task_type=key)
    if not label.strip() or not strategy.strip():
        raise ValueError('教学类型需要名称与策略')
    PROFILES[key] = {'label': label, 'strategy': strategy}


def profile(task):
    return PROFILES.get(task.get('task_type', 'open'), PROFILES['open'])


def validate_requirements(values):
    if any(not value.strip() or len(value) > 200 for value in values):
        raise ValueError('检查要求不能为空，且每项最多200字')
    if len(set(values)) != len(values):
        raise ValueError('检查要求不能重复')
    return values


def check_contract(task):
    """Private assessment contract; old expected answers are not new answer keys."""
    return {
        'task_type': task.get('task_type', 'open'),
        'check_requirements': task.get('check_requirements', []),
        'original_rubric': task['rubric'],
        'criteria': [{k: c.get(k, '') for k in ('id', 'kind', 'description', 'concept_id')}
                     for c in task.get('criteria', [])],
    }


def bind_check_contract(candidate, original):
    """Require dimension coverage; retain new expected values, never copy old ones."""
    from harness.grading.criteria import Criterion, bind_algebra
    expected = original.get('criteria', [])
    rows = [Criterion.model_validate(c).model_dump() for c in candidate.get('criteria', [])]
    if len({c['id'] for c in rows}) != len(rows):
        raise ValueError('检查题评分项编号重复')
    if expected:
        wanted = {c['id']: c for c in expected}
        actual = {c['id']: c for c in rows}
        if actual.keys() != wanted.keys():
            raise ValueError('检查题必须覆盖原任务的全部评分项')
        for key, old in wanted.items():
            new = actual[key]
            if new['kind'] != old['kind']:
                raise ValueError('检查题不能改变评分项类型')
            new.update(description=old['description'], concept_id=old.get('concept_id', ''))
    for row in rows:
        if row['kind'] in ('numeric', 'unit', 'factorization', 'source_quote', 'word_count') and not row['expected']:
            raise ValueError('检查题缺少新题对应的评分预期值')
    candidate.update(criteria=rows, task_type=original.get('task_type', 'open'),
                     check_requirements=original.get('check_requirements', []),
                     check_contract=check_contract(original))
    candidate.update(bind_algebra(candidate['question'], candidate.get('algebra_initial', ''),
                                 candidate.get('algebra_kind', 'none')))
    if any(c['kind'] == 'algebra' for c in rows) and not candidate['algebra_initial']:
        raise ValueError('检查题缺少对应的代数验证器')
    return candidate
