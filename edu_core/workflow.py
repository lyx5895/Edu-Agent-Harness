"""Teacher workflow (design doc 4): objectives -> questions -> grading -> analysis -> practice -> comparison.

Every step reads confirmed upstream artifacts through a whitelisted :class:`Context`, calls the model
outside any transaction, then writes artifacts, references and generation records in one transaction.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from edu_core import artifacts as A
from edu_core import grading
from edu_core.config import Settings
from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import Store, event, new_id, record_generation, row
from edu_core.subjects import KEYED_TYPES, QUESTION_TYPES, SUGGESTED_ONLY_TYPES, pack
from edu_core.text import dump, sha, verbatim_quote

Conflict = A.Conflict
QuestionType = Literal[tuple(QUESTION_TYPES)]  # type: ignore[valid-type]


# ---------- model output schemas ----------


class ObjectiveDraft(BaseModel):
    text: str = Field(max_length=200)
    success_criterion: str = Field(max_length=300)


class PointDraft(BaseModel):
    description: str = Field(max_length=300)
    score: float = Field(gt=0, le=60)
    check_kind: Literal['none', 'numeric', 'algebra', 'chem_equation']
    check_expected: str = Field(max_length=120)


class QuestionDraft(BaseModel):
    type: QuestionType
    stem: str = Field(max_length=3000)
    options: list[str]
    answer_key: str = Field(max_length=200)
    accepted: list[str]
    reference: str = Field(max_length=3000)
    objective_index: int
    target_pattern_index: int
    points: list[PointDraft]


class QuestionSet(BaseModel):
    questions: list[QuestionDraft]


class ImportedPaper(BaseModel):
    objectives: list[ObjectiveDraft]
    questions: list[QuestionDraft]


class SolveOutput(BaseModel):
    answer: str = Field(max_length=4000)
    final_answer: str = Field(max_length=300)


class Evidence(BaseModel):
    eid: str
    quote: str = Field(max_length=300)


class PatternDraft(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(max_length=400)
    tags: list[str]
    evidence: list[Evidence]
    existing_pattern: str


class FollowUp(BaseModel):
    question: str = Field(max_length=400)
    why: str = Field(max_length=300)


class AnalysisOutput(BaseModel):
    summary: str = Field(max_length=800)
    patterns: list[PatternDraft]
    insufficient: list[FollowUp]


class Pitfall(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(max_length=400)
    question_nos: list[int]


class Activity(BaseModel):
    name: str = Field(max_length=60)
    minutes: int = Field(ge=0, le=90)
    description: str = Field(max_length=400)
    objective_index: int


class LessonPlanOutput(BaseModel):
    objectives: list[ObjectiveDraft]
    key_points: list[str]
    difficulties: list[str]
    activities: list[Activity]
    notes: str = Field(max_length=600)


class ReviewSection(BaseModel):
    pattern_index: int
    title: str = Field(max_length=80)
    key_steps: list[str]
    typical_quote: str = Field(max_length=300)
    error_analysis: str = Field(max_length=800)
    follow_up: list[str]


class ReviewOutput(BaseModel):
    focus: list[str]
    sections: list[ReviewSection]
    closing: str = Field(max_length=600)


class PitfallOutput(BaseModel):
    pitfalls: list[Pitfall]


# ---------- prompts ----------

GENERATE_SYSTEM = """你是高中{subject}命题老师，根据已确认的教学目标出题。
要求：
1. 每道题对应一个教学目标，objective_index 是输入中目标的序号（从 0 开始）。target_pattern_index 填 -1。
2. 题型只能从这些题型中选：{types}。
3. 每道题必须给出参考答案 reference 和至少一个评分要点 points（不能是空列表），要点要具体、可检验，分值之和就是本题满分。
   - 选择题和判断题：只需一个评分要点，answer_key 填答案（如 “B”、“AC”、“对”）。
   - 填空题：answer_key 填标准答案，accepted 填其他可接受的写法。
   - 其他题型：answer_key 填空字符串，accepted 填空列表。
4. 最终结果是数值的要点可以设 check_kind=numeric，check_expected 写带单位的最终结果（如 “15 km/h”）；代数式结果设 algebra；化学方程式设 chem_equation；其他填 none 且 check_expected 填空字符串。
5. 题目要原创，不要照抄材料中的原题。options 只在选择题中填写，每个选项不要带字母前缀；其他题型填空列表。{variant}
只输出 JSON。"""

VARIANTS = {
    'a': '',
    'b': '\n6. 本次请优先设计贴近真实情境的题目，并尽量从不同角度考查同一目标。',
}

PRACTICE_SYSTEM = """你是高中{subject}老师，针对已确认的学生问题模式出巩固练习题。
要求：
1. 每道题针对一个问题模式，target_pattern_index 是输入中问题模式的序号（从 0 开始）；objective_index 填对应目标的序号，没有对应目标填 -1。
2. 题目要能检验这个问题是否仍然存在：换情境、换数据，不要重复原题。
3. 题型只能从这些题型中选：{types}。
4. 每道题必须有参考答案 reference 和至少一个评分要点 points（不能是空列表），分值之和为本题满分。选择题和判断题给一个评分要点并填 answer_key；填空题填 answer_key 和 accepted；数值结果可设 check_kind=numeric；其他题型 answer_key 填空字符串、check_kind 填 none。
只输出 JSON。"""

IMPORT_SYSTEM = """你是高中{subject}教研员。老师提供了一份现成的试卷（可能附有答案），请整理成结构化题目。
要求：
1. 保留题目原文，不要改写；options 只在选择题中填写，每个选项不带字母前缀。
2. 试卷附有答案就使用试卷答案；没有就起草参考答案。每道题必须有至少一个可检验的评分要点，分值之和为该题满分（试卷标明分值时按试卷分值）。
3. 根据这些题目反推 1 到 5 条教学目标（objectives），每道题的 objective_index 指向对应目标。target_pattern_index 填 -1。
   如果输入里已有备课方案的教学目标，题目考查的是其中某条就直接用它的序号（已有目标排在前面，序号从 0 开始），只为没有覆盖的内容新增目标。
4. answer_key、accepted、check_kind 的要求：选择判断题给 answer_key；填空题给 answer_key 和 accepted；数值结果可设 numeric；其他填 none。
只输出 JSON。"""

SOLVE_SYSTEM = """你是认真的高中{subject}学生。独立完成下面这道题。
answer 写完整解答过程；final_answer 只写最终答案：选择题只写选项字母（如 B 或 AC），判断题只写“对”或“错”，其他题写最终结论。
只输出 JSON。"""

ANALYZE_SYSTEM = """你是高中{subject}教研员，根据已确认的批改结果做学情分析。
规则：
1. 只能根据输入中的作答证据归纳“问题模式”。每个问题模式至少引用 1 条证据：eid 必须来自输入，quote 必须逐字复制该证据中的学生原文片段。
2. 优先沿用已有的问题模式：同一个问题 existing_pattern 填已有模式的编号；确实是新问题才新建，existing_pattern 填空字符串。
3. tags 填这个问题模式对应的错误标签（来自输入）。
4. 如果某个区分证据不足（例如分不清是“应用时遗漏”还是“概念本身理解错误”），不要下结论，在 insufficient 中写一道能区分两者的补查问题，并说明原因。
   面向老师的文字（summary、description、why）不要出现 E1 这样的证据编号，用学生代号或题号指代。
5. summary 用两三句话概括本轮学情，不写泛泛的教学建议。
只输出 JSON。"""

LESSON_PLAN_SYSTEM = """你是经验丰富的高中{subject}教师，为这一课时写一份备课方案。
要求：
1. objectives 写 2 到 4 条可检验的教学目标：每条写成学生能做到的具体行为（例如“能在解对数不等式时检查真数大于零”），success_criterion 写明怎样从学生作答判断是否达成。
2. key_points 写本课重点（2 到 4 条），difficulties 写学生容易卡住的难点（1 到 3 条），都要具体到本课内容，并结合学生基础。
3. activities 按课堂顺序安排 3 到 6 个活动，写明名称、用时（分钟）、做法；objective_index 指向对应目标的序号（从 0 开始），没有对应目标填 -1。总用时不超过课时长度（默认约 40 到 45 分钟）。
4. notes 写需要提醒老师的事项，没有就填空字符串。
以课时信息为准；参考材料只用来确定内容范围，不要照抄。只输出 JSON。"""

REVIEW_SYSTEM = """你是高中{subject}教师，根据已确认的学情分析为下一节课准备针对性的讲评。
要求：
1. focus 按讲解先后列出本次讲评的重点（2 到 4 条）。
2. sections 每个问题模式一节：pattern_index 是输入中问题模式的序号（从 0 开始）；key_steps 写这类题的关键步骤和每一步的依据；typical_quote 从该问题模式的“学生原文”中逐字复制一段作为典型错误（没有合适的就填空字符串）；error_analysis 说明这段作答错在哪里、为什么会这样错、怎样避免；follow_up 写 2 到 3 个课堂追问，用来检查学生是否真正理解。
3. closing 写一段简短的总结或过渡。
4. 面向学生讲解，不点名批评，不出现学生代号。只输出 JSON。"""

PITFALL_SYSTEM = """你是高中{subject}教研员。目前还没有学生作答，请根据题目和评分要点列出学生“可能出现的易错点”。
这些只是预判，不是本班学情结论。question_nos 填相关题号。只输出 JSON。"""


# ---------- helpers ----------


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _rubric_from(points: list[PointDraft], question_lineage: str, target_pattern=None):
    rows = []
    for i, p in enumerate(points, 1):
        point = {'id': f'P{i}', 'description': p.description, 'score': float(p.score)}
        if p.check_kind != 'none' and p.check_expected.strip():
            point['check'] = {'kind': p.check_kind, 'expected': p.check_expected.strip()}
        if target_pattern:
            point['target_pattern'] = target_pattern
        rows.append(point)
    return {'question_lineage': question_lineage, 'total': round(sum(r['score'] for r in rows), 2), 'points': rows}


def usable_drafts(drafts, allowed, n_patterns=None):
    """Keep drafts the server can use; keyed questions without points get a default point. Returns (kept, dropped)."""
    kept, dropped = [], []
    for d in drafts:
        if d.type not in allowed:
            dropped.append(f'题型 {d.type} 不在允许范围内：{d.stem[:30]}')
            continue
        if n_patterns is not None and not 0 <= d.target_pattern_index < n_patterns:
            dropped.append(f'没有对应到问题模式：{d.stem[:30]}')
            continue
        if not d.points:
            if d.type in KEYED_TYPES and d.answer_key.strip():
                d = d.model_copy(
                    update={
                        'points': [PointDraft(description='答案正确', score=2, check_kind='none', check_expected='')]
                    }
                )
            else:
                dropped.append(f'缺少评分要点：{d.stem[:30]}')
                continue
        kept.append(d)
    return kept, dropped


def _question_content(d: QuestionDraft, no: int, round_: int, objective_ids, target_patterns=()):
    keyed = d.type in KEYED_TYPES or d.type == 'fill_blank'
    return {
        'no': no,
        'type': d.type,
        'stem': d.stem.strip(),
        'options': d.options if d.type in ('single_choice', 'multi_choice') else [],
        'answer_key': d.answer_key.strip() if keyed else '',
        'accepted': [a for a in d.accepted if a.strip()] if d.type == 'fill_blank' else [],
        'reference': d.reference.strip(),
        'objective_ids': list(objective_ids),
        'target_patterns': list(target_patterns),
        'round': round_,
    }


def _step(label, tab, target, hint='', optional=False):
    """A next-step suggestion: the UI jumps to ``tab`` and highlights the element ``target``."""
    step = {'label': label, 'tab': tab, 'target': target, 'hint': hint}
    if optional:
        step['optional'] = True  # shown as a side link; never blocks "接着做"
    return step


def next_steps(st):
    """What the teacher should do next, following the loop 备课设计 → 出题改题 → 作业批改 → 学情分析 → 讲题答疑 → 讲评后再练.

    ``st`` carries project-level counts and the counts of the latest round ``r``; later rounds restart at 出题改题.
    Pending A/B choices and stale results come first.
    """
    steps = []
    if st['pending_ab']:
        steps.append(_step('选择 A/B 候选题', 'questions', '#ab-panel', '先在两版候选题中选一版'))
    if st['stale']:
        steps.append(
            _step('处理依据已变化的产物', 'records', '#stale-list', f'{len(st["stale"])} 个产物的上游有实质修改')
        )
    c, rc, r = st['counts'], st['round'], st['r']
    label_r = f'（第 {r} 轮）' if r > 1 else ''
    if not c['lesson_plan']['confirmed']:
        if c['lesson_plan']['total']:
            steps.append(
                _step('确认备课方案', 'prep', '#lesson-plan', '核对目标、重点难点和课堂活动，整体确认后用于出题')
            )
        elif not st['has_context']:
            steps.append(_step('填写课时信息', 'prep', '#course-module', '至少填写知识模块，或添加参考材料'))
        else:
            steps.append(
                _step('生成备课方案', 'prep', '#draft-lesson-plan', '目标、重点难点和课堂活动一起生成，之后可以编辑')
            )
        return steps
    if not rc['question']['confirmed']:
        if rc['question']['total']:
            steps.append(_step('确认题目' + label_r, 'questions', '#questions', '核对题目、答案和评分要点后确认'))
        else:
            steps.append(_step('按目标出题', 'questions', '#generate-questions', '也可以导入现成试卷或手工添加题目'))
        return steps
    if not rc['submission']['total']:
        if not st['learners']:
            steps.append(_step('添加学生名单', 'grading', '#roster', '每行一个学生代号，不要写真实姓名'))
        else:
            steps.append(_step('导入学生作答' + label_r, 'grading', '#sub-csv', '每行：学生代号,题号,作答'))
    elif rc['grading']['total'] < rc['submission']['total']:
        steps.append(_step('依据标准批改' + label_r, 'grading', '#grade-round'))
    elif st['queue']:
        steps.append(_step('处理复核队列', 'grading', '#queue', f'{st["queue"]} 份批改需要老师判定'))
    elif rc['grading']['confirmed'] < rc['grading']['total']:
        steps.append(_step('确认批改', 'grading', '#confirm-auto', '复核队列以外的批改可以批量确认'))
    elif not rc['analysis']['total']:
        steps.append(_step('汇总批改结果' + label_r, 'analysis', '#analyze'))
    else:
        if r > 1 and not rc['comparison']['total']:
            steps.append(_step(f'对比第 {r - 1}、{r} 轮', 'analysis', '#compare', '看讲评和再练之后问题是否还在'))
        if rc['problem_pattern']['total'] and not rc['problem_pattern']['confirmed']:
            steps.append(_step('确认问题模式', 'analysis', '#patterns', '确认后才能据此准备讲评'))
        elif not rc['problem_pattern']['confirmed']:
            steps.append(_step('查看学情分析', 'analysis', '#analysis', '这一轮没有归纳出需要讲评的问题'))
        elif not rc['review_plan']['total']:
            steps.append(_step('定位讲解重点，准备讲评', 'review', '#draft-review'))
        elif not rc['review_plan']['confirmed']:
            steps.append(_step('确认讲评稿', 'review', '#review-plans', '编辑后确认'))
        else:
            steps.append(_step('讲评后再练', 'review', '#practice', '针对这些问题出一轮再练题，回到出题改题'))
    return steps


def stage_status(counts, rc, r):
    """Which loop stages are done for the latest round (drives the step navigation and "接着做")."""
    graded = rc['grading']['total'] and rc['grading']['confirmed'] == rc['grading']['total']
    return {
        'prep': bool(counts['lesson_plan']['confirmed']),
        'questions': bool(rc['question']['confirmed']),
        'grading': bool(rc['submission']['total'] and graded and rc['grading']['total'] >= rc['submission']['total']),
        'analysis': bool(
            rc['analysis']['total'] and (rc['problem_pattern']['confirmed'] or not rc['problem_pattern']['total'])
        ),
        'review': bool(rc['review_plan']['confirmed']),
        'round': r,
    }


def normalize_plan(plan):
    """Validate a lesson plan; objectives keep stable ids (O1, O2, ...) across edits, new ones get the next id."""
    objectives, used = [], set()
    raw = plan.get('objectives') or []
    taken = [int(m.group(1)) for o in raw if (m := re.fullmatch(r'O(\d+)', str(o.get('id', ''))))]
    nxt = max(taken, default=0)
    for o in raw:
        text = str(o.get('text', '')).strip()
        if not text:
            continue
        oid = str(o.get('id') or '')
        if not re.fullmatch(r'O\d+', oid) or oid in used:
            nxt += 1
            oid = f'O{nxt}'
        used.add(oid)
        item = {'id': oid, 'text': text[:200], 'success_criterion': str(o.get('success_criterion', '')).strip()[:300]}
        if o.get('inferred'):
            item['inferred'] = True
        objectives.append(item)
    if not objectives:
        raise ValueError('备课方案至少需要一条教学目标')
    activities = []
    for a in plan.get('activities') or []:
        name = str(a.get('name', '')).strip()
        if not name:
            continue
        activities.append(
            {
                'name': name[:60],
                'minutes': max(0, min(int(a.get('minutes') or 0), 90)),
                'description': str(a.get('description', '')).strip()[:400],
                'objective_ids': [x for x in a.get('objective_ids', []) if x in used],
            }
        )

    def lines(key):
        return [str(x).strip() for x in plan.get(key) or [] if str(x).strip()]

    return {
        'objectives': objectives,
        'key_points': lines('key_points'),
        'difficulties': lines('difficulties'),
        'activities': activities,
        'notes': str(plan.get('notes', '')).strip()[:600],
    }


class Workspace:
    def __init__(self, store: Store, llm, settings: Settings, rng=None):
        self.store = store
        self.llm = llm
        self.settings = settings
        self.rng = rng or random.Random()

    # ----- plumbing -----

    def _model(self):
        if self.llm is None:
            raise ModelUnavailable('未配置模型（设置 EDU_LLM_API_KEY 后重启服务）')
        return self.llm

    def _project(self, c, pid):
        p = row(c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone())
        if p is None:
            raise KeyError(f'项目不存在：{pid}')
        p['pack'] = pack(p['subject'])
        return p

    def _project_view(self, p):
        keys = (
            'id',
            'title',
            'subject',
            'grade',
            'notes',
            'knowledge_module',
            'lesson',
            'learner_profile',
            'created_at',
        )
        return {k: p.get(k, '') for k in keys} | {
            'subject_name': p['pack']['name'],
            'question_types': p['pack']['types'],
        }

    def _parallel(self, fn, items):
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(self.settings.concurrency, len(items)))) as pool:
            return list(pool.map(fn, items))

    # ----- projects and materials -----

    def create_project(
        self, title, subject, grade='', notes='', owner='', knowledge_module='', lesson='', learner_profile=''
    ):
        if not title.strip():
            raise ValueError('请填写项目名称')
        sp = pack(subject)
        pid = new_id('proj')
        with self.store.tx() as c:
            c.execute(
                'INSERT INTO projects(id,title,subject,grade,notes,owner,created_at,knowledge_module,lesson,learner_profile) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    pid,
                    title.strip(),
                    sp['id'],
                    grade.strip(),
                    notes.strip(),
                    owner,
                    time.time(),
                    knowledge_module.strip(),
                    lesson.strip(),
                    learner_profile.strip(),
                ),
            )
            event(c, pid, 'project_created', {'title': title, 'subject': sp['id']})
            return self._project_view(self._project(c, pid))

    def update_project(
        self,
        pid,
        *,
        title=None,
        subject=None,
        grade=None,
        notes=None,
        knowledge_module=None,
        lesson=None,
        learner_profile=None,
    ):
        """Edit project info. The subject can only change before any question exists (questions depend on it)."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            changes = {}
            if title is not None:
                if not title.strip():
                    raise ValueError('请填写项目名称')
                changes['title'] = title.strip()
            if grade is not None:
                changes['grade'] = grade.strip()
            if notes is not None:
                changes['notes'] = notes.strip()
            for key, value in (
                ('knowledge_module', knowledge_module),
                ('lesson', lesson),
                ('learner_profile', learner_profile),
            ):
                if value is not None:
                    changes[key] = value.strip()[:500]
            if subject is not None and pack(subject)['id'] != p['subject']:
                has_questions = c.execute(
                    "SELECT 1 FROM artifacts WHERE project_id=? AND type='question' LIMIT 1", (pid,)
                ).fetchone()
                if has_questions:
                    raise Conflict('项目里已经有题目，不能再改学科；请新建一个项目')
                changes['subject'] = pack(subject)['id']
            for key, value in changes.items():
                c.execute(f'UPDATE projects SET {key}=? WHERE id=?', (value, pid))
            if changes:
                event(c, pid, 'project_updated', changes)
            return self._project_view(self._project(c, pid))

    def projects(self):
        with self.store.read() as c:
            return [
                self._project_view(self._project(c, r['id']))
                for r in c.execute('SELECT id FROM projects ORDER BY created_at DESC')
            ]

    def add_material(self, pid, text, filename='粘贴文本', kind='text', family_of=None):
        text = (text or '').strip()
        if not text:
            raise ValueError('材料内容为空')
        if len(text) > 200_000:
            raise ValueError('材料过长（上限 20 万字）')
        with self.store.tx() as c:
            self._project(c, pid)
            family = family_of or new_id('fam')
            mid = new_id('mat')
            c.execute(
                'INSERT INTO materials VALUES(?,?,?,?,?,?,?,?)',
                (mid, pid, family, kind, filename, sha(text), text, time.time()),
            )
            event(c, pid, 'material_added', {'material_id': mid, 'family_id': family, 'filename': filename})
            return {'id': mid, 'family_id': family, 'filename': filename, 'chars': len(text)}

    def materials(self, pid):
        with self.store.read() as c:
            return [
                {**dict(r), 'chars': len(r['text']), 'text': r['text'][:400]}
                for r in c.execute('SELECT * FROM materials WHERE project_id=? ORDER BY created_at', (pid,))
            ]

    def _material_context(self, c, ctx, pid, limit=8000):
        out, used = [], 0
        for r in c.execute('SELECT * FROM materials WHERE project_id=? ORDER BY created_at', (pid,)):
            ctx.add_other('material', r['id'], r['sha256'], family_id=r['family_id'], filename=r['filename'])
            piece = r['text'][: max(0, limit - used)]
            used += len(piece)
            out.append({'文件': r['filename'], '内容': piece})
        return out

    # ----- generic artifact operations -----

    def artifact(self, aid):
        with self.store.read() as c:
            a = A.get(c, aid)
            a['stale'] = A.is_stale(c, aid)
            if a['generation_id']:
                g = row(c.execute('SELECT * FROM generations WHERE id=?', (a['generation_id'],)).fetchone())
                a['context_note'] = describe(g['context_manifest']) if g else ''
                a['generation'] = (
                    {k: g[k] for k in ('task', 'model_requested', 'model_returned', 'usage')} if g else None
                )
            history = c.execute(
                'SELECT id, version, status, origin, created_at FROM artifacts WHERE lineage_id=? ORDER BY version',
                (a['lineage_id'],),
            )
            a['history'] = [dict(h) for h in history]
            return a

    def edit(self, aid, content, teacher='teacher'):
        with self.store.tx() as c:
            src = A.get(c, aid)
            if src['type'] in ('grading', 'analysis', 'comparison'):
                raise Conflict('批改请用“改判”，分析和对比请重新生成')
            content = self._validate_content(src['type'], content, src['content'])
            return A.edit(c, aid, content, teacher=teacher)

    def confirm(self, aid, change_kind='substantive', teacher='teacher'):
        with self.store.tx() as c:
            a = A.get(c, aid)
            if a['type'] == 'question':
                rubric = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                if rubric is None:
                    raise Conflict('这道题还没有评分标准')
            done = A.confirm(c, aid, change_kind=change_kind, teacher=teacher)
            if a['type'] == 'question':
                # A question and its rubric are confirmed together: grading needs both.
                rubric = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                if rubric and rubric['status'] != 'confirmed':
                    A.confirm(c, rubric['id'], change_kind=change_kind, teacher=teacher)
            return done

    def create_question(self, pid, question, rubric, teacher='teacher', round_=1):
        with self.store.tx() as c:
            p = self._project(c, pid)
            question = self._validate_content('question', question, {'round': round_})
            if question['type'] not in p['pack']['types']:
                raise ValueError(f'{p["pack"]["name"]}不支持题型：{question["type"]}')
            question['no'] = self._next_no(c, pid, round_)
            q = A.create(c, pid, 'question', question, origin='teacher', created_by=teacher, round_=round_)
            rubric = self._validate_content('rubric', {**rubric, 'question_lineage': q['lineage_id']}, {})
            A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin='teacher',
                refs=[(q['id'], 'question')],
                created_by=teacher,
                round_=round_,
            )
            return q

    def _validate_content(self, type_, content, previous):
        if not isinstance(content, dict):
            raise ValueError('内容格式错误')
        merged = {**previous, **content}
        if type_ == 'lesson_plan':
            merged = normalize_plan(merged)
        elif type_ == 'question':
            if merged.get('type') not in QUESTION_TYPES:
                raise ValueError('未知题型')
            if not str(merged.get('stem', '')).strip():
                raise ValueError('题干不能为空')
            merged.setdefault('options', [])
            merged.setdefault('answer_key', '')
            merged.setdefault('accepted', [])
            merged.setdefault('reference', '')
            merged.setdefault('objective_ids', [])
            merged.setdefault('target_patterns', [])
            if merged['type'] in KEYED_TYPES and not merged['answer_key'].strip():
                raise ValueError('选择题和判断题需要填写答案')
        elif type_ == 'rubric':
            points = merged.get('points') or []
            if not points:
                raise ValueError('评分标准至少需要一个评分要点')
            clean = []
            for i, p in enumerate(points, 1):
                score = float(p.get('score', 0))
                if score <= 0 or not str(p.get('description', '')).strip():
                    raise ValueError(f'第 {i} 个评分要点缺少描述或分值')
                item = {'id': p.get('id') or f'P{i}', 'description': p['description'].strip(), 'score': score}
                for key in ('check', 'target_pattern'):
                    if p.get(key):
                        item[key] = p[key]
                clean.append(item)
            if len({p['id'] for p in clean}) != len(clean):
                raise ValueError('评分要点编号重复')
            merged['points'] = clean
            merged['total'] = round(sum(p['score'] for p in clean), 2)
        return merged

    def _next_no(self, c, pid, round_):
        return (
            c.execute(
                "SELECT COUNT(DISTINCT lineage_id) FROM artifacts WHERE project_id=? AND type='question' AND round=? AND status!='candidate'",
                (pid, round_),
            ).fetchone()[0]
            + 1
        )

    def _rubric_for(self, c, question_lineage, include_drafts=False):
        """The rubric of a question lineage: latest confirmed version, or (include_drafts) latest version."""
        statuses = ('confirmed', 'draft', 'candidate') if include_drafts else ('confirmed',)
        marks = ','.join('?' * len(statuses))
        rows = c.execute(
            f"SELECT * FROM artifacts WHERE type='rubric' AND status IN ({marks}) "
            "AND json_extract(content, '$.question_lineage')=? ORDER BY created_at DESC, version DESC LIMIT 1",
            (*statuses, question_lineage),
        ).fetchone()
        return row(rows)

    # ----- questions -----

    def _plan(self, c, pid, *, confirmed=True):
        """The project's lesson plan: its confirmed version, or (confirmed=False) its latest version."""
        plans = A.current(c, pid, 'lesson_plan')
        if not plans:
            return None
        return A.confirmed_in_lineage(c, plans[0]['lineage_id']) if confirmed else plans[0]

    @staticmethod
    def _goals(plan):
        """Plan objectives as handles: ``id`` is the plan artifact (for refs), ``lineage_id`` the stable objective id."""
        if not plan:
            return []
        return [{'id': plan['id'], 'lineage_id': o['id'], 'content': o} for o in plan['content']['objectives']]

    def _confirmed_objectives(self, c, pid, objective_ids=None):
        goals = self._goals(self._plan(c, pid))
        if objective_ids:
            goals = [g for g in goals if g['lineage_id'] in set(objective_ids)]
        return goals

    def generate_questions(
        self, pid, *, types=None, count=2, difficulty='中等', notes='', objective_ids=None, ab=None, purpose='课堂练习'
    ):
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            plan = self._plan(c, pid)
            if plan is None:
                raise Conflict('请先确认备课方案，再按目标出题')
            objs = self._confirmed_objectives(c, pid, objective_ids)
            if not objs:
                raise Conflict('备课方案里没有选中的教学目标')
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            ctx.add_artifact(plan)
            goal_list = [
                {'index': i, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                for i, o in enumerate(objs)
            ]
            materials = self._material_context(c, ctx, pid, limit=6000)
        allowed = [t for t in (types or p['pack']['types']) if t in p['pack']['types']]
        if not allowed:
            raise ValueError('所选题型不属于该学科')
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': goal_list,
                '本课重点': plan['content'].get('key_points', []),
                '本课难点': plan['content'].get('difficulties', []),
                '学生基础': p.get('learner_profile', ''),
                '材料摘录': materials,
                '允许题型': [QUESTION_TYPES[t] + f'（{t}）' for t in allowed],
                '每个目标出题数': max(1, min(int(count), 5)),
                '练习用途': purpose,
                '难度': difficulty,
                '老师说明': notes or p['notes'],
            }
        )
        use_ab = ab if ab is not None else self.rng.random() < self.settings.ab_rate
        variants = ['a', 'b'] if use_ab else ['a']
        if use_ab:
            self.rng.shuffle(variants)  # randomize which variant is shown as A

        def run(variant):
            system = GENERATE_SYSTEM.format(
                subject=p['pack']['name'],
                types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed),
                variant=VARIANTS[variant],
            )
            out, meta = llm.complete('generator', 'generate_questions', system, user, QuestionSet)
            drafts, dropped = usable_drafts(out.questions, allowed)
            checks = self._parallel(lambda d: self._self_check(p, d), drafts)
            return variant, meta, list(zip(drafts, checks)), dropped

        runs = [run(v) for v in variants]
        group = new_id('ab') if use_ab else None
        result = {'context_note': describe(ctx.manifest), 'candidate_group': group, 'candidates': []}
        with self.store.tx() as c:
            for position, (variant, meta, items, dropped) in zip('AB', runs):
                gid = record_generation(
                    c,
                    pid,
                    meta,
                    ctx.manifest,
                    'generate-v1',
                    group=group,
                    position=position if group else None,
                    variant=variant,
                )
                made = self._save_questions(
                    c, pid, items, objs, gid, status='candidate' if group else 'draft', round_=1
                )
                result['candidates'].append(
                    {
                        'position': position,
                        'variant': variant,
                        'generation_id': gid,
                        'questions': made,
                        'dropped': dropped,
                    }
                )
        return result

    def _self_check(self, p, d: QuestionDraft):
        """Another model solves the question without the key; the grader checks that answer (design doc 4 ②)."""
        subject = p['subject']
        checks = {'notes': [], '_metas': []}
        for x in d.points:
            if x.check_kind == 'numeric' and x.check_expected.strip():
                from edu_core.tools import check_numeric

                ok, _ = check_numeric(d.reference, x.check_expected)
                if ok is False:
                    checks['notes'].append(f'参考答案的最终数值与核对值 {x.check_expected} 不一致')
                    checks['reference_mismatch'] = True
        if d.type in SUGGESTED_ONLY_TYPES or self.llm is None:
            checks['solver'] = 'skipped'
            return checks
        try:
            solve_ctx = Context('self_check_solve')
            solve_ctx.add_other('question_stem', sha(d.stem))
            solved, meta = self.llm.complete(
                'reviewer',
                'self_check_solve',
                SOLVE_SYSTEM.format(subject=p['pack']['name']),
                _json(
                    {
                        '题型': QUESTION_TYPES[d.type],
                        '题目': d.stem,
                        '选项': [f'{"ABCDEFGH"[i]}. {o}' for i, o in enumerate(d.options[:8])],
                    }
                ),
                SolveOutput,
            )
            question = _question_content(d, 0, 1, [])
            rubric = _rubric_from(d.points, 'self-check')
            # Keyed and fill-blank questions compare the final answer; other types grade the full solution.
            answer = solved.final_answer if d.type in KEYED_TYPES or d.type == 'fill_blank' else solved.answer
            res, grade_metas = grading.grade(
                self.llm, subject=subject, question=question, rubric=rubric, answer=answer, review=False
            )
            checks['_metas'] = [meta, *grade_metas]
            agree = res['undetermined'] == 0 and res['total'] >= res['max_total'] - 1e-9
            checks.update(
                solver='agree' if agree else 'disagree',
                solver_answer=solved.answer[:2000],
                solver_final=solved.final_answer,
                solver_score=f'{res["total"]}/{res["max_total"]}',
                solver_model=meta.get('model_returned'),
            )
            if not agree:
                checks['notes'].append('另一个模型独立解题的结果与参考答案不一致，请核对答案')
        except ModelUnavailable as e:
            checks.update(solver='error', notes=[*checks['notes'], f'自检失败：{e}'])
        checks['answer_suspect'] = checks.get('solver') == 'disagree' or bool(checks.get('reference_mismatch'))
        return checks

    def _save_questions(self, c, pid, items, objs, gid, *, status, round_, patterns=(), origin='generated'):
        made = []
        no = self._next_no(c, pid, round_)
        for d, checks in items:
            checks = dict(checks)
            metas = checks.pop('_metas', [])
            manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
            checks['generation_ids'] = [record_generation(c, pid, m, manifest, 'self-check-v1') for m in metas]
            obj = objs[d.objective_index] if 0 <= d.objective_index < len(objs) else None
            pattern = patterns[d.target_pattern_index] if 0 <= d.target_pattern_index < len(patterns) else None
            content = _question_content(
                d, no, round_, [obj['lineage_id']] if obj else [], [pattern['lineage_id']] if pattern else []
            )
            content['checks'] = checks
            refs = ([(obj['id'], 'lesson_plan')] if obj else []) + (
                [(pattern['id'], 'problem_pattern')] if pattern else []
            )
            q = A.create(
                c, pid, 'question', content, origin=origin, refs=refs, generation_id=gid, status=status, round_=round_
            )
            rubric = _rubric_from(d.points, q['lineage_id'], pattern['lineage_id'] if pattern else None)
            r = A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin=origin,
                refs=[(q['id'], 'question'), *refs],
                generation_id=gid,
                status=status,
                round_=round_,
            )
            q['rubric'] = r
            made.append(q)
            if status != 'candidate':
                no += 1
        return made

    def choose(self, pid, group, verdict, reason='', teacher='teacher'):
        """A/B choice: A, B, same (adopt A) or neither. Unchosen candidates stay as research records."""
        if verdict not in ('A', 'B', 'same', 'neither'):
            raise ValueError('选择应为 A、B、same（差不多）或 neither（都不合适）')
        with self.store.tx() as c:
            gens = {
                r['position']: r['id']
                for r in c.execute(
                    'SELECT id, position FROM generations WHERE project_id=? AND candidate_group=?', (pid, group)
                )
            }
            if not gens:
                raise KeyError('候选组不存在')
            if c.execute('SELECT 1 FROM choices WHERE candidate_group=?', (group,)).fetchone():
                raise Conflict('这一组已经选择过了')
            chosen = {'A': 'A', 'B': 'B', 'same': 'A'}.get(verdict)
            c.execute(
                'INSERT INTO choices VALUES(?,?,?,?,?,?,?,?)',
                (new_id('cho'), pid, group, verdict, chosen, reason, teacher, time.time()),
            )
            promoted = []
            if chosen:
                rows = c.execute(
                    "SELECT id, type FROM artifacts WHERE generation_id=? AND status='candidate' ORDER BY created_at",
                    (gens[chosen],),
                ).fetchall()
                no = self._next_no(c, pid, 1)
                for r in rows:
                    if r['type'] == 'question':
                        a = A.get(c, r['id'])
                        content = {**a['content'], 'no': no}
                        no += 1
                        c.execute(
                            'UPDATE artifacts SET status=?, content=?, content_sha=? WHERE id=?',
                            ('draft', dump(content), sha(content), r['id']),
                        )
                    else:
                        c.execute("UPDATE artifacts SET status='draft' WHERE id=?", (r['id'],))
                    promoted.append(r['id'])
            event(c, pid, 'ab_choice', {'group': group, 'verdict': verdict, 'reason': reason})
            return {'verdict': verdict, 'promoted': promoted}

    def import_paper(self, pid, text, filename='导入试卷'):
        llm = self._model()
        material = self.add_material(pid, text, filename=filename, kind='paper')
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('import_paper')
            ctx.add_other('project', pid)
            ctx.add_other(
                'material', material['id'], sha(text.strip()), family_id=material['family_id'], filename=filename
            )
        with self.store.read() as c:
            current_plan = self._plan(c, pid, confirmed=False)
        existing = current_plan['content']['objectives'] if current_plan else []
        system = IMPORT_SYSTEM.format(subject=p['pack']['name'])
        out, meta = llm.complete(
            'generator',
            'import_paper',
            system,
            _json({'学科': p['pack']['name'], '已有教学目标': [o['text'] for o in existing], '试卷': text[:30000]}),
            ImportedPaper,
        )
        allowed = set(QUESTION_TYPES)
        drafts, dropped = usable_drafts(out.questions, allowed)
        checks = [{'notes': ['由导入试卷整理，评分要点和教学目标为反推，请核对'], 'imported': True} for _ in drafts]
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'import-v1')
            known = {o['text'].strip() for o in existing}
            added = [
                {'text': o.text, 'success_criterion': o.success_criterion, 'inferred': True}
                for o in out.objectives
                if o.text.strip() not in known
            ]
            base = (
                current_plan['content']
                if current_plan
                else {'key_points': [], 'difficulties': [], 'activities': [], 'notes': ''}
            )
            content = normalize_plan({**base, 'objectives': [*existing, *added]})
            plan = A.create(
                c,
                pid,
                'lesson_plan',
                content,
                origin='imported',
                generation_id=gid,
                lineage_id=current_plan['lineage_id'] if current_plan else None,
            )
            objs = self._goals(plan)  # existing objectives first, so the model's indexes line up
            made = self._save_questions(
                c, pid, list(zip(drafts, checks)), objs, gid, status='draft', round_=1, origin='imported'
            )
        return {
            'lesson_plan': plan,
            'objectives': [o['content'] for o in objs],
            'questions': made,
            'dropped': dropped,
            'context_note': describe(ctx.manifest),
        }

    def questions(self, pid, round_=None):
        with self.store.read() as c:
            qs = A.current(c, pid, 'question', round_=round_)
            for q in qs:
                q['rubric'] = self._rubric_for(c, q['lineage_id'], include_drafts=True)
                if q['rubric']:
                    q['rubric']['stale'] = A.is_stale(c, q['rubric']['id'])
                    q['rubric']['confirmed_version'] = (A.confirmed_in_lineage(c, q['rubric']['lineage_id']) or {}).get(
                        'version'
                    )
                if q['generation_id']:
                    g = c.execute(
                        'SELECT candidate_group, position, variant FROM generations WHERE id=?', (q['generation_id'],)
                    ).fetchone()
                    q['candidate'] = dict(g) if g and g['candidate_group'] else None
            return qs

    # ----- roster and submissions -----

    def add_learners(self, pid, aliases):
        added = []
        with self.store.tx() as c:
            self._project(c, pid)
            for alias in aliases:
                alias = alias.strip()
                if not alias:
                    continue
                if len(alias) > 40:
                    raise ValueError(f'学生代号过长：{alias[:10]}…')
                cur = c.execute(
                    'INSERT OR IGNORE INTO learners VALUES(?,?,?,?)', (new_id('lrn'), pid, alias, time.time())
                )
                if cur.rowcount:
                    added.append(alias)
        return {'added': added, 'learners': self.learners(pid)}

    def learners(self, pid):
        with self.store.read() as c:
            return [
                dict(r)
                for r in c.execute('SELECT id, alias FROM learners WHERE project_id=? ORDER BY created_at', (pid,))
            ]

    def import_submissions(self, pid, csv_text, round_=1, add_missing=False):
        """CSV rows: 学生代号,题号,作答. Learners are matched against the roster; unmatched rows are reported."""
        reader = csv.reader(io.StringIO(csv_text.strip()))
        rows = [r for r in reader if r and any(x.strip() for x in r)]
        if rows and (len(rows[0]) < 2 or not rows[0][1].strip().isdigit()):
            rows = rows[1:]  # header
        if not rows:
            raise ValueError('没有可导入的作答（每行：学生代号,题号,作答）')
        with self.store.tx() as c:
            self._project(c, pid)
            questions = {
                a['content']['no']: A.confirmed_in_lineage(c, a['lineage_id'])
                for a in A.current(c, pid, 'question', round_=round_)
                if a['confirmed_version']
            }
            roster = {
                r['alias']: r['id'] for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))
            }
            created, unmatched, problems = [], set(), []
            for i, r in enumerate(rows, 1):
                if len(r) < 3:
                    problems.append(f'第 {i} 行格式不对')
                    continue
                alias, no, text = r[0].strip(), r[1].strip(), ','.join(r[2:]).strip()
                if alias not in roster:
                    if not add_missing:
                        unmatched.add(alias)
                        continue
                    roster[alias] = new_id('lrn')
                    c.execute('INSERT INTO learners VALUES(?,?,?,?)', (roster[alias], pid, alias, time.time()))
                q = questions.get(int(no)) if no.isdigit() else None
                if q is None:
                    problems.append(f'第 {i} 行：第 {no} 题不存在或尚未确认')
                    continue
                content = {
                    'learner_id': roster[alias],
                    'learner_alias': alias,
                    'question_lineage': q['lineage_id'],
                    'question_no': int(no),
                    'text': text,
                }
                lineage = self._submission_lineage(c, pid, roster[alias], q['lineage_id'], round_)
                created.append(
                    A.create(
                        c,
                        pid,
                        'submission',
                        content,
                        origin='imported',
                        refs=[(q['id'], 'question')],
                        status='confirmed',
                        created_by='teacher',
                        round_=round_,
                        lineage_id=lineage,
                        change_kind='substantive',
                    )['id']
                )
            event(c, pid, 'submissions_imported', {'count': len(created), 'round': round_})
        return {'created': len(created), 'unmatched_learners': sorted(unmatched), 'problems': problems}

    def _submission_lineage(self, c, pid, learner_id, question_lineage, round_):
        for a in A.listing(c, pid, 'submission', round_=round_):
            if a['content']['learner_id'] == learner_id and a['content']['question_lineage'] == question_lineage:
                return a['lineage_id']
        return None

    def submissions(self, pid, round_=1):
        with self.store.read() as c:
            return A.current(c, pid, 'submission', round_=round_)

    # ----- grading -----

    def grade_round(self, pid, round_=1, regrade=False):
        llm = self._model()
        jobs, skipped = [], []
        with self.store.read() as c:
            p = self._project(c, pid)
            graded = {}
            for g in A.current(c, pid, 'grading', round_=round_):
                graded[g['content']['submission_lineage']] = g
            for s in A.current(c, pid, 'submission', round_=round_):
                q = A.confirmed_in_lineage(c, s['content']['question_lineage'])
                rubric = self._rubric_for(c, s['content']['question_lineage'])
                if q is None or rubric is None:
                    skipped.append({'submission': s['id'], 'why': '题目或评分标准尚未确认'})
                    continue
                prior = graded.get(s['lineage_id'])
                # Only substantive upstream changes (open stale marks) trigger a regrade; cosmetic edits do not.
                if prior is not None and not A.is_stale(c, prior['id']) and not regrade:
                    continue
                ctx = Context('grade')
                question = ctx.add_artifact(q)
                rub = ctx.add_artifact(rubric)
                sub = ctx.add_artifact(s)
                jobs.append((ctx, question, rub, sub, s, prior))

        def run(job):
            _ctx, question, rub, sub, _s, _prior = job
            try:
                result, metas = grading.grade(
                    llm,
                    subject=p['subject'],
                    question=question,
                    rubric=rub,
                    answer=sub['text'],
                    review=True,
                    audit_rate=self.settings.audit_rate,
                    rng=self.rng,
                )
                return job, result, metas, None
            except ModelUnavailable as e:
                return job, None, [], str(e)

        outcomes = self._parallel(run, jobs)
        made, failed = [], []
        with self.store.tx() as c:
            for (ctx, _question, _rub, sub, s, prior), result, metas, error in outcomes:
                if error:
                    failed.append({'learner': sub['learner_alias'], 'question_no': sub['question_no'], 'error': error})
                    continue
                gids = [record_generation(c, pid, m, ctx.manifest, grading.PROMPT_VERSION) for m in metas]
                content = {
                    'submission_id': s['id'],
                    'submission_lineage': s['lineage_id'],
                    'learner_id': sub['learner_id'],
                    'learner_alias': sub['learner_alias'],
                    'question_lineage': sub['question_lineage'],
                    'question_no': sub['question_no'],
                    'generation_ids': gids,
                    **result,
                }
                g = A.create(
                    c,
                    pid,
                    'grading',
                    content,
                    origin='generated',
                    refs=ctx.refs(),
                    generation_id=gids[0] if gids else None,
                    status='draft',
                    round_=round_,
                    lineage_id=prior['lineage_id'] if prior else None,
                )
                if prior:
                    c.execute(
                        "UPDATE stale_marks SET status='resolved', resolution='redone', resolved_at=? WHERE artifact_id=? AND status='open'",
                        (time.time(), prior['id']),
                    )
                made.append(g['id'])
        return {
            'graded': len(made),
            'failed': failed,
            'skipped': skipped,
            'review_queue': len(self.review_queue(pid, round_)),
        }

    def gradings(self, pid, round_=1):
        with self.store.read() as c:
            return A.current(c, pid, 'grading', round_=round_)

    def review_queue(self, pid, round_=1):
        with self.store.read() as c:
            queue = []
            for g in A.current(c, pid, 'grading', round_=round_):
                if g['status'] != 'draft' or not g['content'].get('needs_teacher'):
                    continue
                s = A.get(c, g['content']['submission_id'])
                q = A.confirmed_in_lineage(c, g['content']['question_lineage'])
                queue.append({**g, 'answer': s['content']['text'], 'question': q['content'] if q else None})
            return sorted(queue, key=lambda g: (-len(g['content']['risks']), g['content']['question_no']))

    def override(self, grading_id, points, note='', teacher='teacher'):
        """Teacher grading: new confirmed version; downstream analyses become stale."""
        with self.store.tx() as c:
            g = A.get(c, grading_id)
            if g['type'] != 'grading':
                raise ValueError('只能改判批改结果')
            if g['status'] == 'superseded':
                raise Conflict('这条批改已有更新的版本，请刷新')
            content = json.loads(dump(g['content']))
            by_id = {p['id']: p for p in content['points']}
            for change in points:
                p = by_id.get(change.get('id'))
                if p is None:
                    raise ValueError(f'未知评分要点：{change.get("id")}')
                score = float(change['score'])
                if not 0 <= score <= p['max']:
                    raise ValueError(f'{p["id"]} 的分数应在 0 到 {p["max"]} 之间')
                status = 'earned' if score >= p['max'] else ('missed' if score == 0 else 'partial')
                p.update(
                    score=score,
                    status=status,
                    source='teacher',
                    confidence='high',
                    error_tag=change.get('error_tag', p.get('error_tag', '')) if status != 'earned' else '',
                    error_note=change.get('error_note', p.get('error_note', '')) if status != 'earned' else '',
                    reason=change.get('reason') or '老师判定',
                )
                if status != 'earned' and not p['error_tag']:
                    p['error_tag'] = '其他'
            if any(p['status'] == 'undetermined' for p in content['points']):
                raise ValueError('还有未判定的评分要点，请全部给分后再提交')
            content.update(
                total=round(sum(p['score'] for p in content['points']), 2),
                undetermined=0,
                risks=[],
                needs_teacher=False,
                teacher_note=note,
                overridden_from=grading_id,
            )
            return A.create(
                c,
                g['project_id'],
                'grading',
                content,
                origin='teacher',
                refs=[(r['ref_id'], r['role']) for r in g['refs']],
                lineage_id=g['lineage_id'],
                status='confirmed',
                created_by=teacher,
                round_=g['round'],
                edit_of=grading_id,
                change_kind='substantive',
            )

    def confirm_gradings(self, pid, round_=1, ids=None, teacher='teacher'):
        """Confirm gradings; without ids, every draft that is not in the review queue."""
        done = []
        with self.store.tx() as c:
            for g in A.current(c, pid, 'grading', round_=round_):
                if g['status'] != 'draft':
                    continue
                if ids is None and g['content'].get('needs_teacher'):
                    continue
                if ids is not None and g['id'] not in ids:
                    continue
                if g['content'].get('undetermined'):
                    raise Conflict(
                        f'{g["content"]["learner_alias"]} 第 {g["content"]["question_no"]} 题还有未判定的要点，请先改判'
                    )
                A.confirm(c, g['id'], teacher=teacher)
                done.append(g['id'])
        return {'confirmed': len(done)}

    # ----- analysis -----

    def analyze(self, pid, round_=1):
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            subs = A.current(c, pid, 'submission', round_=round_)
            current_gradings = A.current(c, pid, 'grading', round_=round_)
            confirmed = [A.confirmed_in_lineage(c, g['lineage_id']) for g in current_gradings if g['confirmed_version']]
            confirmed = [g for g in confirmed if g]
            questions = [
                A.confirmed_in_lineage(c, q['lineage_id'])
                for q in A.current(c, pid, 'question', round_=round_)
                if q['confirmed_version']
            ]
            if subs and not confirmed:
                raise Conflict('还没有已确认的批改，请先确认批改结果（可以批量确认）')
            ctx = Context('pitfalls' if not subs else 'analyze')
            ctx.add_other('project', pid)
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            objectives = [g['content']['text'] for g in self._goals(plan)]
            q_view = []
            for q in questions:
                ctx.add_artifact(q)
                rubric = self._rubric_for(c, q['lineage_id'])
                if rubric:
                    ctx.add_artifact(rubric)
                q_view.append(
                    {
                        '题号': q['content']['no'],
                        '题目': q['content']['stem'][:500],
                        '评分要点': [
                            x['description'] for x in (rubric or {'content': {'points': []}})['content']['points']
                        ],
                    }
                )
            existing = []
            if subs:
                for pat in A.current(c, pid, 'problem_pattern'):
                    if pat['confirmed_version']:
                        conf = A.confirmed_in_lineage(c, pat['lineage_id'])
                        ctx.add_artifact(conf)
                        existing.append(
                            {
                                '编号': conf['lineage_id'],
                                '名称': conf['content']['name'],
                                '描述': conf['content']['description'],
                            }
                        )
                answers = {}
                for g in confirmed:
                    ctx.add_artifact(g)
                    s = A.get(c, g['content']['submission_id'])
                    ctx.add_artifact(s)
                    answers[g['id']] = s['content']['text']
        if not subs:
            if not q_view:
                raise Conflict('还没有已确认的题目，无法分析')
            system = PITFALL_SYSTEM.format(subject=p['pack']['name'])
            out, meta = llm.complete(
                'analyst', 'pitfalls', system, _json({'教学目标': objectives, '题目': q_view}), PitfallOutput
            )
            with self.store.tx() as c:
                gid = record_generation(c, pid, meta, ctx.manifest, 'pitfalls-v1')
                content = {
                    'mode': 'pitfalls',
                    'note': '未基于学生作答，只是可能的易错点',
                    'pitfalls': [x.model_dump() for x in out.pitfalls],
                    'round': round_,
                }
                a = A.create(
                    c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), generation_id=gid, round_=round_
                )
            return {'analysis': a, 'patterns': [], 'context_note': describe(ctx.manifest)}

        evidence, by_tag, learners_all = {}, {}, set()
        stats = {}
        for g in confirmed:
            content = g['content']
            learners_all.add(content['learner_alias'])
            qs = stats.setdefault(content['question_no'], {'n': 0, 'score': 0.0, 'max': 0.0, 'points': {}})
            qs['n'] += 1
            qs['score'] += content['total']
            qs['max'] += content['max_total']
            for pt in content['points']:
                ps = qs['points'].setdefault(pt['id'], {'description': pt['description'], 'score': 0.0, 'max': 0.0})
                ps['score'] += pt['score'] or 0
                ps['max'] += pt['max']
                if pt['status'] in ('partial', 'missed'):
                    eid = f'E{len(evidence) + 1}'
                    quote = pt.get('evidence_quote') or answers[g['id']][:150]
                    evidence[eid] = {
                        'eid': eid,
                        'grading_id': g['id'],
                        'submission_id': content['submission_id'],
                        'learner': content['learner_alias'],
                        'question_no': content['question_no'],
                        'point_id': pt['id'],
                        'point': pt['description'],
                        'tag': pt.get('error_tag') or '其他',
                        'note': pt.get('error_note', ''),
                        'quote': quote,
                        'target_pattern': pt.get('target_pattern'),
                    }
                    by_tag.setdefault(evidence[eid]['tag'], []).append(eid)
        tag_summary = [
            {
                '错误标签': tag,
                '涉及学生数': len({evidence[e]['learner'] for e in eids}),
                '证据': [
                    {
                        'eid': e,
                        '题号': evidence[e]['question_no'],
                        '要点': evidence[e]['point'],
                        '学生原文': evidence[e]['quote'][:200],
                        '备注': evidence[e]['note'],
                    }
                    for e in eids[:10]
                ],
            }
            for tag, eids in sorted(by_tag.items(), key=lambda kv: -len(kv[1]))
        ]
        stats_view = {
            no: {
                '人数': s['n'],
                '得分率': round(s['score'] / s['max'], 3) if s['max'] else None,
                '要点得分率': {
                    pid_: round(x['score'] / x['max'], 3) if x['max'] else None for pid_, x in s['points'].items()
                },
            }
            for no, s in sorted(stats.items())
        }
        if not evidence:
            content = {
                'mode': 'evidence',
                'round': round_,
                'stats': stats_view,
                'summary': '本轮所有已确认的批改都没有失分，没有可归纳的问题。',
                'patterns': [],
                'insufficient': [],
                'learners': len(learners_all),
            }
            with self.store.tx() as c:
                a = A.create(
                    c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), round_=round_, created_by='server'
                )
            return {'analysis': a, 'patterns': [], 'context_note': describe(ctx.manifest)}
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': objectives,
                '题目': q_view,
                '得分统计': stats_view,
                '错误汇总': tag_summary,
                '已有问题模式': existing,
            }
        )
        out, meta = llm.complete(
            'analyst', 'analyze', ANALYZE_SYSTEM.format(subject=p['pack']['name']), user, AnalysisOutput
        )
        existing_ids = {e['编号'] for e in existing}
        patterns, dropped = [], []
        for pat in out.patterns:
            valid = []
            for ev in pat.evidence:
                item = evidence.get(ev.eid)
                if item is None:
                    continue
                quote = verbatim_quote(ev.quote, answers[item['grading_id']]) if ev.quote else None
                if quote:
                    valid.append({**item, 'quote': quote})
            if not valid:
                dropped.append(pat.name)
                continue
            tags = [t for t in pat.tags if t in by_tag]
            affected = {v['learner'] for v in valid} | {evidence[e]['learner'] for t in tags for e in by_tag[t]}
            patterns.append(
                {
                    'name': pat.name,
                    'description': pat.description,
                    'tags': tags,
                    'evidence': [
                        {
                            k: v[k]
                            for k in (
                                'eid',
                                'learner',
                                'question_no',
                                'point_id',
                                'quote',
                                'submission_id',
                                'grading_id',
                            )
                        }
                        for v in valid
                    ],
                    'affected_learners': sorted(affected),
                    'total_learners': len(learners_all),
                    'round': round_,
                    'lineage': pat.existing_pattern if pat.existing_pattern in existing_ids else None,
                }
            )
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'analyze-v1')
            content = {
                'mode': 'evidence',
                'round': round_,
                'stats': stats_view,
                'summary': out.summary,
                'insufficient': [x.model_dump() for x in out.insufficient],
                'dropped_patterns': dropped,
                'learners': len(learners_all),
                'tag_counts': {t: len({evidence[e]['learner'] for e in eids}) for t, eids in by_tag.items()},
            }
            analysis = A.create(
                c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), generation_id=gid, round_=round_
            )
            made = []
            for pat in patterns:
                lineage = pat.pop('lineage')
                refs = [(analysis['id'], 'analysis')] + [
                    (gid_, 'grading') for gid_ in sorted({e['grading_id'] for e in pat['evidence']})
                ]
                made.append(
                    A.create(
                        c,
                        pid,
                        'problem_pattern',
                        pat,
                        origin='generated',
                        refs=refs,
                        generation_id=gid,
                        lineage_id=lineage,
                        round_=round_,
                    )
                )
        return {'analysis': analysis, 'patterns': made, 'context_note': describe(ctx.manifest)}

    def analyses(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'analysis')

    def patterns(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'problem_pattern')

    # ----- 01 lesson plan (重点难点与课堂活动) -----

    def draft_lesson_plan(self, pid):
        """备课方案: objectives with success criteria, key points, difficulties and timed activities, confirmed as one."""
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('draft_lesson_plan')
            ctx.add_other('project', pid)
            materials = self._material_context(c, ctx, pid, limit=8000)
            if not (p.get('knowledge_module') or materials):
                raise Conflict('请先填写课时信息中的知识模块，或添加参考材料')
            prior = self._plan(c, pid, confirmed=False)
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '知识模块': p.get('knowledge_module', ''),
                '课时': p.get('lesson', ''),
                '学生基础': p.get('learner_profile', ''),
                '老师说明': p['notes'],
                '项目名称': p['title'],
                '参考材料': materials,
            }
        )
        out, meta = llm.complete(
            'generator',
            'draft_lesson_plan',
            LESSON_PLAN_SYSTEM.format(subject=p['pack']['name']),
            user,
            LessonPlanOutput,
        )
        if not out.objectives:
            raise ModelUnavailable('模型没有给出教学目标，请重试')
        content = normalize_plan(
            {
                'objectives': [o.model_dump() for o in out.objectives],
                'key_points': out.key_points,
                'difficulties': out.difficulties,
                'activities': [
                    {
                        **a.model_dump(exclude={'objective_index'}),
                        'objective_ids': [f'O{a.objective_index + 1}']
                        if 0 <= a.objective_index < len(out.objectives)
                        else [],
                    }
                    for a in out.activities
                ],
                'notes': out.notes,
            }
        )
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'lesson-plan-v2')
            plan = A.create(
                c,
                pid,
                'lesson_plan',
                content,
                origin='generated',
                refs=ctx.refs(),
                generation_id=gid,
                lineage_id=prior['lineage_id'] if prior else None,
            )
        return {'lesson_plan': plan, 'context_note': describe(ctx.manifest)}

    def lesson_plans(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'lesson_plan')

    # ----- 05 讲题答疑 -----

    def _round_patterns(self, c, pid, round_):
        """Confirmed problem patterns as they were in ``round_`` (a later round may add a newer version)."""
        best = {}
        for a in A.listing(c, pid, 'problem_pattern', statuses=('confirmed', 'superseded'), round_=round_):
            if a['status'] == 'superseded' and not a['change_kind']:
                continue  # a superseded draft was never adopted
            if a['lineage_id'] not in best or a['version'] > best[a['lineage_id']]['version']:
                best[a['lineage_id']] = a
        return sorted(best.values(), key=lambda a: a['created_at'])

    def draft_review(self, pid, round_=None, pattern_ids=None):
        """讲评稿: focus, key steps, typical errors quoted from students, and follow-up questions per pattern."""
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            round_ = round_ or self.max_round(pid)
            pats = self._round_patterns(c, pid, round_)
            if pattern_ids:
                pats = [x for x in pats if x['id'] in pattern_ids or x['lineage_id'] in pattern_ids]
            if not pats:
                raise Conflict(f'第 {round_} 轮还没有已确认的问题模式，请先在学情分析中确认')
            ctx = Context('draft_review')
            ctx.add_other('project', pid)
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            goals = [g['content']['text'] for g in self._goals(plan)]
            key_points = plan['content'].get('key_points', []) if plan else []
            analyses = [a for a in A.current(c, pid, 'analysis', round_=round_)]
            summary = ''
            if analyses:
                ctx.add_artifact(analyses[-1], require_confirmed=False)
                summary = analyses[-1]['content'].get('summary', '')
            nos = sorted({e['question_no'] for x in pats for e in x['content']['evidence']})
            originals = []
            for q in A.current(c, pid, 'question', round_=round_):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                if conf and conf['content']['no'] in nos:
                    rubric = self._rubric_for(c, q['lineage_id'])
                    originals.append(
                        {
                            '题号': conf['content']['no'],
                            '题目': ctx.add_artifact(conf)['stem'][:600],
                            '参考答案': conf['content']['reference'][:600],
                            '评分要点': [x['description'] for x in ctx.add_artifact(rubric)['points']]
                            if rubric
                            else [],
                        }
                    )
            pat_list = [
                {
                    'index': i,
                    '名称': ctx.add_artifact(x, require_confirmed=x['status'] == 'confirmed')['name'],
                    '描述': x['content']['description'],
                    '涉及人数': f'{len(x["content"]["affected_learners"])}/{x["content"]["total_learners"]}',
                    '学生原文': [e['quote'] for e in x['content']['evidence'][:4]],
                }
                for i, x in enumerate(pats)
            ]
            prior = next(iter(A.current(c, pid, 'review_plan', round_=round_)), None)
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': goals,
                '本课重点': key_points,
                '学情概括': summary,
                '原题': originals,
                '问题模式': pat_list,
            }
        )
        out, meta = llm.complete(
            'generator', 'draft_review', REVIEW_SYSTEM.format(subject=p['pack']['name']), user, ReviewOutput
        )
        sections, dropped = [], []
        for sec in out.sections:
            if not 0 <= sec.pattern_index < len(pats):
                dropped.append(sec.title)
                continue
            pat = pats[sec.pattern_index]
            quote, source = '', None
            if sec.typical_quote.strip():
                for e in pat['content']['evidence']:
                    found = verbatim_quote(sec.typical_quote, e['quote'])
                    if found:
                        quote, source = found, {'question_no': e['question_no'], 'point_id': e['point_id']}
                        break
            sections.append(
                {
                    'pattern_lineage': pat['lineage_id'],
                    'pattern_name': pat['content']['name'],
                    'title': sec.title,
                    'key_steps': [x for x in sec.key_steps if x.strip()],
                    'typical_quote': quote,
                    'typical_source': source,
                    'quote_rejected': bool(sec.typical_quote.strip()) and not quote,
                    'error_analysis': sec.error_analysis,
                    'follow_up': [x for x in sec.follow_up if x.strip()],
                }
            )
        content = {
            'round': round_,
            'focus': out.focus,
            'sections': sections,
            'closing': out.closing,
            'dropped': dropped,
        }
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'review-plan-v1')
            plan = A.create(
                c,
                pid,
                'review_plan',
                content,
                origin='generated',
                refs=ctx.refs(),
                generation_id=gid,
                round_=round_,
                lineage_id=prior['lineage_id'] if prior else None,
            )
        return {'review_plan': plan, 'context_note': describe(ctx.manifest)}

    def review_plans(self, pid, round_=None):
        with self.store.read() as c:
            return A.current(c, pid, 'review_plan', round_=round_)

    # ----- 讲评后再练 and comparison -----

    def max_round(self, pid):
        with self.store.read() as c:
            return c.execute('SELECT COALESCE(MAX(round),1) FROM artifacts WHERE project_id=?', (pid,)).fetchone()[0]

    def practice(self, pid, pattern_ids=None, count=1, types=None, round_=None):
        """讲评后再练: questions targeting the confirmed problem patterns of ``round_`` (default: latest), as a new round."""
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            round_ = round_ or self.max_round(pid)
            pats = self._round_patterns(c, pid, round_)
            if pattern_ids:
                pats = [x for x in pats if x['id'] in pattern_ids or x['lineage_id'] in pattern_ids]
            if not pats:
                raise Conflict(f'第 {round_} 轮还没有已确认的问题模式，请先在学情分析中确认')
            ctx = Context('practice')
            ctx.add_other('project', pid)
            review = next((r for r in A.current(c, pid, 'review_plan', round_=round_) if r['confirmed_version']), None)
            review_focus = []
            if review:
                review = A.confirmed_in_lineage(c, review['lineage_id'])
                review_focus = ctx.add_artifact(review)['focus']
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            objs = self._goals(plan)
            goal_list = [{'index': i, '目标': o['content']['text']} for i, o in enumerate(objs)]
            pat_list = [
                {
                    'index': i,
                    '名称': ctx.add_artifact(x, require_confirmed=x['status'] == 'confirmed')['name'],
                    '描述': x['content']['description'],
                    '学生原文示例': [e['quote'] for e in x['content']['evidence'][:3]],
                }
                for i, x in enumerate(pats)
            ]
            nos = sorted({e['question_no'] for x in pats for e in x['content']['evidence']})
            originals = []
            for q in A.current(c, pid, 'question', round_=round_):
                if q['confirmed_version'] and q['content']['no'] in nos:
                    conf = A.confirmed_in_lineage(c, q['lineage_id'])
                    originals.append({'题号': conf['content']['no'], '题目': ctx.add_artifact(conf)['stem'][:500]})
            new_round = self.max_round(pid) + 1
        allowed = [
            t for t in (types or p['pack']['types']) if t in p['pack']['types'] and t not in SUGGESTED_ONLY_TYPES
        ]
        system = PRACTICE_SYSTEM.format(
            subject=p['pack']['name'], types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed)
        )
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': goal_list,
                '问题模式': pat_list,
                '原题': originals,
                '讲评重点': review_focus,
                '每个问题模式出题数': max(1, min(int(count), 3)),
            }
        )
        out, meta = llm.complete('generator', 'practice', system, user, QuestionSet)
        drafts, dropped = usable_drafts(out.questions, allowed, n_patterns=len(pats))
        checks = self._parallel(lambda d: self._self_check(p, d), drafts)
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'practice-v1')
            made = self._save_questions(
                c, pid, list(zip(drafts, checks)), objs, gid, status='draft', round_=new_round, patterns=pats
            )
        return {'round': new_round, 'questions': made, 'dropped': dropped, 'context_note': describe(ctx.manifest)}

    def compare(self, pid, round_a=1, round_b=2):
        with self.store.read() as c:
            pats = self._round_patterns(c, pid, round_a)
            gradings_b = [
                A.confirmed_in_lineage(c, g['lineage_id'])
                for g in A.current(c, pid, 'grading', round_=round_b)
                if g['confirmed_version']
            ]
            gradings_b = [g for g in gradings_b if g]
        if not pats:
            raise Conflict(f'第 {round_a} 轮还没有已确认的问题模式')
        if not gradings_b:
            raise Conflict(f'第 {round_b} 轮还没有已确认的批改')
        rows = []
        for pat in pats:
            before = set(pat['content']['affected_learners'])
            answered, still = set(), set()
            for g in gradings_b:
                for pt in g['content']['points']:
                    if pt.get('target_pattern') != pat['lineage_id'] and pt.get('error_tag') not in pat['content'].get(
                        'tags', []
                    ):
                        continue
                    if pt.get('target_pattern') == pat['lineage_id']:
                        answered.add(g['content']['learner_alias'])
                    if pt['status'] in ('partial', 'missed'):
                        still.add(g['content']['learner_alias'])
            rows.append(
                {
                    'pattern_id': pat['id'],
                    'pattern_lineage': pat['lineage_id'],
                    'name': pat['content']['name'],
                    'before': sorted(before),
                    'retested': sorted(answered),
                    'improved': sorted((before & answered) - still),
                    'persisting': sorted(before & still),
                    'new': sorted(still - before),
                    'not_retested': sorted(before - answered),
                    'enough_evidence': bool(answered),
                }
            )
        with self.store.tx() as c:
            content = {'round_a': round_a, 'round_b': round_b, 'patterns': rows}
            refs = [(x['id'], 'problem_pattern') for x in pats] + [(g['id'], 'grading') for g in gradings_b]
            a = A.create(
                c, pid, 'comparison', content, origin='generated', refs=refs, round_=round_b, created_by='server'
            )
        return a

    # ----- staleness, overview, export -----

    def stale(self, pid):
        with self.store.read() as c:
            return A.stale(c, pid)

    def resolve_stale(self, mark_id, resolution):
        with self.store.tx() as c:
            A.resolve_stale(c, mark_id, resolution)
        return {'ok': True}

    def overview(self, pid):
        with self.store.read() as c:
            p = self._project_view(self._project(c, pid))
            counts = {}
            for t in A.TYPES:
                items = A.current(c, pid, t)
                counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
            rounds = sorted(
                {r[0] for r in c.execute('SELECT DISTINCT round FROM artifacts WHERE project_id=?', (pid,))}
            ) or [1]
            mats = c.execute('SELECT COUNT(*) FROM materials WHERE project_id=?', (pid,)).fetchone()[0]
            learners = c.execute('SELECT COUNT(*) FROM learners WHERE project_id=?', (pid,)).fetchone()[0]
            pending_ab = [
                r[0]
                for r in c.execute(
                    'SELECT DISTINCT candidate_group FROM generations WHERE project_id=? AND candidate_group IS NOT NULL '
                    'AND candidate_group NOT IN (SELECT candidate_group FROM choices)',
                    (pid,),
                )
            ]
            latest = max(rounds)
            round_counts = {}
            for t in A.TYPES:
                items = A.current(c, pid, t, round_=latest)
                round_counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
        stale = self.stale(pid)
        queue = sum(len(self.review_queue(pid, r)) for r in rounds)
        nxt = next_steps(
            {
                'counts': counts,
                'round': round_counts,
                'r': latest,
                'materials': mats,
                'has_context': bool(mats or p.get('knowledge_module')),
                'learners': learners,
                'queue': len(self.review_queue(pid, latest)),
                'pending_ab': pending_ab,
                'stale': stale,
            }
        )
        return {
            'project': p,
            'counts': counts,
            'rounds': rounds,
            'materials': mats,
            'learners': learners,
            'stale': stale,
            'review_queue': queue,
            'pending_ab': pending_ab,
            'next': nxt,
            'latest_round': latest,
            'stages': stage_status(counts, round_counts, latest),
        }

    def export(self, pid):
        """Research export (design doc 10): artifacts with all versions, references, generations, choices, events."""
        with self.store.read() as c:
            p = self._project_view(self._project(c, pid))
            arts = [row(r) for r in c.execute('SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at', (pid,))]
            refs = [
                dict(r)
                for r in c.execute(
                    'SELECT r.* FROM artifact_refs r JOIN artifacts a ON a.id=r.artifact_id WHERE a.project_id=?',
                    (pid,),
                )
            ]
            gens = [
                row(r) for r in c.execute('SELECT * FROM generations WHERE project_id=? ORDER BY created_at', (pid,))
            ]
            choices = [dict(r) for r in c.execute('SELECT * FROM choices WHERE project_id=?', (pid,))]
            events = [row(r) for r in c.execute('SELECT * FROM events WHERE project_id=? ORDER BY id', (pid,))]
            marks = [dict(r) for r in c.execute('SELECT * FROM stale_marks WHERE project_id=?', (pid,))]
            mats = [dict(r) for r in c.execute('SELECT * FROM materials WHERE project_id=?', (pid,))]
            learners = [dict(r) for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))]
        by_id = {a['id']: a for a in arts}
        edits = []
        for a in arts:
            if a['edit_of'] and a['edit_of'] in by_id:
                before = by_id[a['edit_of']]['content']
                changed = sorted(k for k in set(before) | set(a['content']) if before.get(k) != a['content'].get(k))
                edits.append(
                    {'artifact_id': a['id'], 'edit_of': a['edit_of'], 'origin': a['origin'], 'changed_fields': changed}
                )
        teachers = {a['created_by'] for a in arts if a['created_by'] not in ('model', 'server')}
        manifest = {
            'project': p,
            'exported_at': time.time(),
            'counts': {
                'reviewed_tasks': len({(g['task'], g['candidate_group'] or g['id']) for g in gens}),
                'material_families': len({m['family_id'] for m in mats}),
                'course_projects': 1,
                'teachers': len(teachers),
                'model_responses': len(gens),
                'artifact_versions': len(arts),
                'artifact_lineages': len({a['lineage_id'] for a in arts}),
            },
            'note': '多次回答与多个版本不计为新增案例；同一材料家族的产物应作为一组切分。学生只以代号出现，导出前仍需去标识检查。',
        }
        return {
            'manifest': manifest,
            'materials': mats,
            'learners': learners,
            'artifacts': arts,
            'refs': refs,
            'generations': gens,
            'choices': choices,
            'events': events,
            'stale_marks': marks,
            'edits': edits,
        }
