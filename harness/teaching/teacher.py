"""Teacher-authored, immutable assignment versions and evidence worklists."""

import json
import uuid
from typing import Literal

from pydantic import Field, field_validator, model_validator

from harness.core.pedagogy import TeachingContract, validate_requirements
from harness.grading.criteria import Criterion, bind_algebra, tool_grade


class Assignment(TeachingContract):
    _requirements = field_validator('check_requirements')(validate_requirements)

    class_id: str = Field(min_length=1, max_length=80)
    course_id: str = Field(min_length=1, max_length=80)
    concept_id: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=240)
    domain: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=900)
    rubric: str = Field(min_length=1, max_length=600)
    reference: str = Field(min_length=1, max_length=700)
    material: str = Field(default='', max_length=100000)
    prerequisite: str = Field(default='', max_length=240)
    teaching_note: str = Field(default='', max_length=500)
    allow_explanation: bool = True
    reference_origin: Literal['teacher_authored', 'teacher_reviewed_model', 'model_generated_unverified'] = (
        'teacher_authored'
    )
    check_assignment_ids: list[str] = Field(default_factory=list, max_length=20)
    criteria: list[Criterion] = Field(default_factory=list, max_length=4)
    algebra_initial: str = Field(default='', max_length=160)
    algebra_kind: Literal['none', 'expression', 'equation'] = 'none'

    @model_validator(mode='after')
    def validate_criteria(self):
        if len({x.id for x in self.criteria}) != len(self.criteria):
            raise ValueError('评分项编号不能重复')
        for item in self.criteria:
            if item.kind in ('numeric', 'unit', 'factorization', 'source_quote', 'word_count') and not item.expected:
                raise ValueError('数值、单位和因式分解评分项需要expected')
        return self


def initialize(c):
    c.execute('CREATE TABLE IF NOT EXISTS teaching_assignments(id TEXT PRIMARY KEY, body TEXT NOT NULL)')


def publish(c, data):
    body = Assignment.model_validate(data).model_dump()
    if body['reference_origin'] == 'model_generated_unverified':
        raise ValueError('模型草稿必须经教师审核确认后发布')
    body.update(bind_algebra(body['question'], body['algebra_initial'], body['algebra_kind']))
    if body['algebra_initial']:
        reference_check = tool_grade(
            {'id': 'reference', 'kind': 'algebra', 'description': '参考答案核对'}, body, body['reference']
        )
        if reference_check['status'] == 'incorrect':
            raise ValueError('教师参考答案与题目原式不一致')
    for criterion in body['criteria']:
        from harness.grading.criteria import GRADERS

        if criterion['kind'] not in GRADERS and criterion['kind'] != 'model':
            raise ValueError('未安装评分插件：' + criterion['kind'])
        if criterion['kind'] == 'source_quote' and criterion['expected'] not in body['material']:
            raise ValueError('指定引文必须来自教学材料')
        if criterion['kind'] == 'word_count':
            import re

            bounds = re.fullmatch(r'(\d+):(\d+)', criterion['expected'])
            if not bounds or int(bounds[1]) > int(bounds[2]):
                raise ValueError('长度范围需为最小值:最大值')
        if criterion['kind'] == 'algebra' and not body['algebra_initial']:
            raise ValueError('代数评分项需要可核对的原式')
        result = tool_grade(criterion, body, body['reference'])
        if result['status'] == 'incorrect':
            raise ValueError('参考答案未通过评分项：' + criterion['id'])
    for check_id in body['check_assignment_ids']:
        check = load(c, check_id)
        if any(
            check.get(k, 'open') != body.get(k, 'open') for k in ('class_id', 'course_id', 'concept_id', 'task_type')
        ):
            raise ValueError('检查题须属于同一班级、课程、知识点和教学类型')
        if not set(body['check_requirements']) <= set(check.get('check_requirements', [])):
            raise ValueError('检查题未覆盖独立检查要求')
        required = {(x['id'], x['kind'], x['description']) for x in body['criteria']}
        supplied = {(x['id'], x['kind'], x['description']) for x in check.get('criteria', [])}
        if not required <= supplied:
            raise ValueError('检查题未覆盖原任务的评分项')
    key = uuid.uuid4().hex
    c.execute('INSERT INTO teaching_assignments VALUES(?,?)', (key, json.dumps(body, ensure_ascii=False)))
    return {'assignment_id': key, **body}


def load(c, key):
    r = c.execute('SELECT body FROM teaching_assignments WHERE id=?', (key,)).fetchone()
    if not r:
        raise ValueError('教学任务不存在')
    return json.loads(r['body'])


SUMMARY_FIELDS = ('course_id', 'concept_id', 'domain', 'task_type', 'objective', 'question')


def list_for_class(c, class_id):
    """Teacher-facing summaries of a class's assignments, newest first (no answer keys)."""
    rows = c.execute(
        "SELECT rowid, id, body FROM teaching_assignments WHERE json_extract(body,'$.class_id')=? ORDER BY rowid DESC",
        (class_id,),
    )
    return [
        {'assignment_id': r['id'], **{k: body.get(k, '') for k in SUMMARY_FIELDS}}
        for r in rows
        for body in [json.loads(r['body'])]
    ]
