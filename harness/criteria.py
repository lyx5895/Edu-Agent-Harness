"""Explicit rubric dimensions. Deterministic tools grade only their own dimension."""
import re
from pydantic import BaseModel, Field
from .algebra import expression, equation_solution, verify


class Criterion(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r'^[a-zA-Z0-9_-]+$')
    description: str = Field(min_length=1, max_length=200)
    kind: str = Field(default='model', min_length=1, max_length=60, pattern=r'^[a-z][a-z0-9_-]*$')
    expected: str = Field(default='', max_length=160)
    concept_id: str = Field(default='', max_length=80)
    tolerance: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)


def bind_algebra(question, initial='', kind='none'):
    if not initial:
        matches=re.findall(r'[0-9x.+*/()^×÷−-]+\s*=\s*[0-9x.+*/()^×÷−-]+',question)
        if len(matches)==1 and 'x' in matches[0]: initial=matches[0];kind='equation'
    if not initial:
        return {'algebra_initial':'','algebra_kind':'none','verifier_binding':'unavailable'}
    if re.sub(r'\s','',initial) not in re.sub(r'\s','',question):
        raise ValueError('验证器的原式必须逐字来自题目')
    if kind not in ('expression','equation'): raise ValueError('需要选择代数式或方程类型')
    (expression if kind=='expression' else equation_solution)(initial)
    return {'algebra_initial':initial,'algebra_kind':kind,'verifier_binding':'question_grounded'}


def _builtin_grade(criterion, task, text):
    kind=criterion['kind'];expected=criterion.get('expected','')
    status='unknown';reason='该项需要模型或教师核对'
    final=text.strip().splitlines()[-1].strip()
    if kind=='algebra' and task.get('algebra_initial'):
        r=verify({'initial':task['algebra_initial'],'kind':task['algebra_kind']},text)
        status=r['status'];reason=r['reason']
        # Expression equivalence does not require simplified or factorized form.
        if task['algebra_kind']=='expression' and status=='incomplete': status='correct'
    elif kind=='numeric':
        match=re.fullmatch(r'(?:[a-zA-Z]\s*=\s*)?([-+]?\d+(?:\.\d+)?(?:/\d+)?)\s*(?:[a-zA-Z/²³^0-9]+)?',final)
        if match and expected:
            try:
                delta=abs(expression(match[1])-expression(expected))
                if delta.is_number: status='correct' if delta<=criterion.get('tolerance',0) else 'incorrect'
                reason='比较最终数值；不验证解释和单位'
            except (ValueError,TypeError,SyntaxError): pass
    elif kind=='unit' and expected:
        normalize=lambda x: re.sub(r'\s','',x).replace('²','^2').replace('³','^3')
        match=re.fullmatch(r'(?:[a-zA-Z]\s*=\s*)?[-+]?\d+(?:\.\d+)?\s*([a-zA-Z/²³^0-9]*)',final)
        if match:
            status='incomplete' if not match[1] else 'correct' if normalize(match[1])==normalize(expected) else 'incorrect'
            reason='检查教师指定的最终单位；不做单位换算'
    elif kind=='explanation' and re.fullmatch(r'[0-9x\s=+*/().^;；→\-]+',text):
        status='incomplete';reason='只有算式或结果，缺少要求的解释'
    elif kind=='factorization' and expected:
        try:
            if expression(final)!=expression(expected): status='incorrect'
            else:
                import ast
                normalized=re.sub(r'(\d|x|\))(?=x|\()',r'\1*',final.replace('^','**'))
                node=ast.parse(normalized,mode='eval').body
                # Products of nonconstant factors or a nonconstant squared factor.
                product=isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mult)
                power=isinstance(node,ast.BinOp) and isinstance(node.op,ast.Pow) and isinstance(node.right,ast.Constant) and node.right.value==2
                factors=product and 'x' in ast.unparse(node.left) and 'x' in ast.unparse(node.right)
                status='correct' if factors or power else 'incomplete'
            reason='检查等价性及乘积/幂的表达形式'
        except (ValueError,TypeError,SyntaxError): pass
    return {'id':criterion['id'],'description':criterion['description'],'concept_id':criterion.get('concept_id',''),
            'status':status,'source':'tool' if status!='unknown' else 'unverified','reason':reason}


def aggregate(results):
    statuses={r['status'] for r in results}
    for value in ('incorrect','unknown','incomplete'):
        if value in statuses: return value
    return 'correct' if results else 'unknown'


# Server-owned registry. Plugins cannot be installed or executed by the model.
GRADERS = {}


def register_grader(kind, grader):
    Criterion(id='plugin', description='plugin', kind=kind)
    if not callable(grader):
        raise ValueError('验证器必须可调用')
    GRADERS[kind] = grader


def _source_quote(criterion, task, text):
    expected = criterion.get('expected', '')
    material = task.get('material', '')
    if not expected or expected not in material:
        return {'status': 'unknown', 'reason': '指定引文无法在任务材料中核对'}
    return {'status': 'correct' if expected in text else 'incomplete',
            'reason': '只检查指定原文引文是否出现，不证明推断或论证正确'}


def _word_count(criterion, task, text):
    bounds = re.fullmatch(r'(\d+):(\d+)', criterion.get('expected', ''))
    if not bounds or int(bounds[1]) > int(bounds[2]):
        return {'status': 'unknown', 'reason': '长度范围需为最小值:最大值'}
    count = len(re.findall(r"[a-zA-Z]+(?:['’-][a-zA-Z]+)*|[\u4e00-\u9fff]", text))
    return {'status': 'correct' if int(bounds[1]) <= count <= int(bounds[2]) else 'incomplete',
            'reason': f'按英文单词和中文单字计数：{count}；只判断长度，不判断写作质量'}


for _kind in ('algebra', 'numeric', 'unit', 'explanation', 'factorization'):
    register_grader(_kind, _builtin_grade)
register_grader('source_quote', _source_quote)
register_grader('word_count', _word_count)


def tool_grade(criterion, task, text):
    base = {'id': criterion['id'], 'description': criterion['description'],
            'concept_id': criterion.get('concept_id', ''), 'status': 'unknown',
            'source': 'unverified', 'reason': '该项需要模型或教师核对'}
    grader = GRADERS.get(criterion['kind'])
    if not grader or not text.strip():
        return base
    result = grader(criterion, task, text)
    if result.get('status') not in ('correct', 'incorrect', 'incomplete', 'unknown'):
        raise ValueError('验证器返回无效状态')
    return {**base, 'status': result['status'], 'reason': result.get('reason', ''),
            'source': 'tool' if result['status'] != 'unknown' else 'unverified'}
