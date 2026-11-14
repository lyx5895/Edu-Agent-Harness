"""Editable, versioned preparation packs; atomic publication and class distribution."""

import json
import time
import uuid

from pydantic import BaseModel, Field, model_validator

from harness.errors import Conflict
from harness.teaching import planning, teacher
from harness.teaching.teacher import Assignment


class Pack(BaseModel):
    class_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    items: list[Assignment] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def same_class(self):
        if any(a.class_id != self.class_id for a in self.items):
            raise ValueError('备课包内任务必须属于当前班级；可包含不同课程和学科')
        return self


class GenerationGoal(BaseModel):
    course_id: str = Field(min_length=1, max_length=80)
    concept_id: str = Field(min_length=1, max_length=80)
    domain: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=240)
    task_type: str = Field(default='open', min_length=1, max_length=60)
    check_requirements: list[str] = Field(default_factory=list, max_length=8)


class GeneratePack(BaseModel):
    class_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    material: str = Field(default='', max_length=100000)
    goals: list[GenerationGoal] = Field(min_length=1, max_length=6)


class Distribution(BaseModel):
    learners: list[str] = Field(default_factory=list, max_length=100)
    due_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    review_note: str = Field(min_length=1, max_length=400)
    reviewed: bool = False

    @model_validator(mode='after')
    def valid(self):
        if not self.reviewed:
            raise ValueError('请审核题目、参考答案和评分要求后确认发布')
        if not self.review_note.strip():
            raise ValueError('请填写审核说明')
        if len(set(self.learners)) != len(self.learners) or any(not x.strip() or len(x) > 80 for x in self.learners):
            raise ValueError('学生名单为空、重复或名称过长')
        return self


def initialize(c):
    c.execute(
        'CREATE TABLE IF NOT EXISTS preparation_packs(id TEXT PRIMARY KEY,class_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,result TEXT)'
    )
    c.execute(
        'CREATE TABLE IF NOT EXISTS preparation_events(id INTEGER PRIMARY KEY AUTOINCREMENT,pack_id TEXT,kind TEXT,payload TEXT,at REAL)'
    )
    c.execute('CREATE INDEX IF NOT EXISTS preparation_class ON preparation_packs(class_id,status)')


def event(c, key, kind, payload):
    c.execute(
        'INSERT INTO preparation_events(pack_id,kind,payload,at) VALUES(?,?,?,?)',
        (key, kind, json.dumps(payload, ensure_ascii=False), time.time()),
    )


def load(c, key):
    row = c.execute('SELECT * FROM preparation_packs WHERE id=?', (key,)).fetchone()
    if not row:
        raise ValueError('备课包不存在')
    return {
        'pack_id': row['id'],
        'class_id': row['class_id'],
        'version': row['version'],
        'status': row['status'],
        'body': json.loads(row['body']),
        'result': json.loads(row['result']) if row['result'] else None,
    }


def save(c, data, key=None, version=None):
    body = Pack.model_validate(data).model_dump()
    if key:
        old = load(c, key)
        if old['status'] != 'draft':
            raise Conflict('已发布备课包不可修改，请创建新版本')
        if version != old['version']:
            raise Conflict('备课包已更新，请重新加载')
        if body['class_id'] != old['class_id']:
            raise ValueError('不能更改备课包班级')
        version += 1
        c.execute(
            'UPDATE preparation_packs SET body=?,version=? WHERE id=?',
            (json.dumps(body, ensure_ascii=False), version, key),
        )
    else:
        key = uuid.uuid4().hex
        version = 1
        c.execute(
            'INSERT INTO preparation_packs VALUES(?,?,?,?,?,NULL)',
            (key, body['class_id'], version, 'draft', json.dumps(body, ensure_ascii=False)),
        )
    event(c, key, 'draft_saved', {'version': version, 'body': body})
    return load(c, key)


def publish(c, key, version, distribution):
    p = load(c, key)
    if p['version'] != version or p['status'] != 'draft':
        raise Conflict('草稿已更新或发布，请重新加载')
    d = Distribution.model_validate(distribution).model_dump()
    assignments = []
    followups = []
    for item in p['body']['items']:
        item = dict(item)
        if item.get('reference_origin') == 'model_generated_unverified':
            item['reference_origin'] = 'teacher_reviewed_model'
        a = teacher.publish(c, item)
        assignments.append(a['assignment_id'])
        if d['learners']:
            followups.extend(
                planning.assign(
                    c,
                    dict(
                        class_id=p['class_id'],
                        assignment_id=a['assignment_id'],
                        learners=d['learners'],
                        kind='remediation',
                        due_at=d['due_at'],
                        reason=d['review_note'],
                    ),
                )
            )
    result = {'assignment_ids': assignments, 'distributed_tasks': len(followups), 'review_note': d['review_note']}
    c.execute(
        "UPDATE preparation_packs SET status='published',version=version+1,result=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), key),
    )
    event(c, key, 'published', {'version': version + 1, **result, 'learners': d['learners']})
    return load(c, key)
