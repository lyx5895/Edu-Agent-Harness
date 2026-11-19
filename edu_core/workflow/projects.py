"""Projects, materials, curriculum picks and 进入下一课时 (carry-over into the next lesson)."""

from __future__ import annotations

import json
import re
import time

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core.storage import event, new_id
from edu_core.subjects import pack
from edu_core.text import sha
from edu_core.workflow.common import (
    WEAK_MATCH,
    Conflict,
    _json,
)


class ProjectsMixin:
    """Projects and materials."""

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

    def _curriculum_query(self, p):
        return ' '.join(x for x in (p.get('knowledge_module', ''), p.get('lesson_scope', '')) if x) or p['title']

    def curriculum(self, pid, q=None):
        """对应课标: suggestions for the course info (or a search), and what is selected."""
        with self.store.read() as c:
            p = self._project(c, pid)
        data = CUR.load(p['subject'])
        if not data:
            return {'available': False}
        chosen = json.loads(p['curriculum']) if p.get('curriculum') else None
        query = q if q is not None else self._curriculum_query(p)
        found = CUR.search(p['subject'], query, k=8)
        auto = self._auto_pick(found)
        selected = chosen['ids'] if chosen else [it['id'] for it in auto]
        by_id = data['by_id']
        extra = [by_id[i] for i in selected if i in by_id and i not in {f['id'] for f in found}]

        def view(it):
            return {
                'id': it['id'],
                'label': it['label'],
                'short': CUR.short_label(it),
                'text': it['text'],
                'page': it['page'],
                'optional': bool(it.get('optional')),
            }

        return {
            'available': True,
            'title': data['title'],
            'source_url': data['source_url'],
            'scope': data['scope'],
            'query': query,
            'items': [view(it) for it in extra + found],
            'selected': selected,
            'off': bool(chosen and chosen.get('off')),
            'chosen_by': chosen.get('chosen_by') if chosen else None,
            'weak': not chosen and not auto and bool(found),
        }

    @staticmethod
    def _auto_pick(found):
        if not found or found[0]['score'] < WEAK_MATCH:
            return []  # no clear match: offer the candidates, but do not constrain the plan with a guess
        top = found[0]['score']
        # 选学 items (not examined) are offered but never picked automatically
        return [it for it in found if it['score'] >= 0.75 * top and not it.get('optional')][:4]

    def _curriculum_for_plan(self, c, p):
        """Resolve the standard items for drafting: the teacher's pick, else an automatic pick saved for review."""
        data = CUR.load(p['subject'])
        if not data:
            return None
        chosen = json.loads(p['curriculum']) if p.get('curriculum') else None
        if chosen and chosen.get('off'):
            return None
        if chosen:
            ids = chosen['ids']
        else:
            ids = [it['id'] for it in self._auto_pick(CUR.search(p['subject'], self._curriculum_query(p), k=8))]
            c.execute(
                'UPDATE projects SET curriculum=? WHERE id=?',
                (_json({'ids': ids, 'off': False, 'chosen_by': 'auto'}), p['id']),
            )
        if not ids:
            return None
        return CUR.context(p['subject'], ids, self._curriculum_query(p))

    def _carryover(self, c, pid):
        """What the next lesson inherits from this one: confirmed problem patterns (with the re-practice result
        when there is one), the 学情 summary, the 讲评 focus and this lesson's objectives. Frozen at creation."""
        p = self._project(c, pid)
        trend = {}
        comparisons = A.current(c, pid, 'comparison')
        if comparisons:
            for r in comparisons[-1]['content']['patterns']:
                trend[r['pattern_lineage']] = r
        patterns = []
        for x in A.current(c, pid, 'problem_pattern'):
            conf = A.confirmed_in_lineage(c, x['lineage_id'])
            if not conf:
                continue
            ct = conf['content']
            row_ = {
                'name': ct['name'],
                'description': ct['description'],
                'affected': f"{len(ct['affected_learners'])}/{ct['total_learners']}",
            }
            t = trend.get(x['lineage_id'])
            if t and t['enough_evidence']:
                row_['after_practice'] = (
                    f"再练后仍有 {len(t['persisting'])} 人出错" if t['persisting'] else '再练后已改善'
                )
            else:
                row_['after_practice'] = '还没有再练结果'
            patterns.append(row_)
        analyses = A.current(c, pid, 'analysis')
        reviews = [r for r in A.current(c, pid, 'review_plan') if r['confirmed_version']]
        plan = self._plan(c, pid)
        snapshot = {
            'from_project': pid,
            'from_title': p['title'],
            'from_lesson': p.get('lesson', ''),
            'summary': analyses[-1]['content'].get('summary', '') if analyses else '',
            'patterns': patterns,
            'review_focus': A.confirmed_in_lineage(c, reviews[-1]['lineage_id'])['content'].get('focus', [])
            if reviews
            else [],
            'objectives': [g['content']['text'] for g in self._goals(plan)],
        }
        if not (snapshot['patterns'] or snapshot['summary']):
            raise Conflict('这一课时还没有学情分析结果，先完成“学情分析”再进入下一课时')
        return snapshot

    def next_lesson(self, pid, *, knowledge_module=None, lesson=None, title=None):
        """进入下一课时: a new project that inherits subject, grade, learner profile and roster, and carries this
        lesson's 学情 into its 备课方案."""
        with self.store.read() as c:
            p = self._project(c, pid)
            carry = self._carryover(c, pid)
            roster = [r['alias'] for r in c.execute('SELECT alias FROM learners WHERE project_id=? ORDER BY created_at', (pid,))]
        old_lesson = p.get('lesson', '')
        if lesson is None:
            m = re.search(r'第\s*(\d+)\s*课时', old_lesson)
            lesson = re.sub(r'第\s*\d+\s*课时', f'第 {int(m.group(1)) + 1} 课时', old_lesson) if m else ''
        module = p.get('knowledge_module', '') if knowledge_module is None else knowledge_module
        if not title:
            base = p['title']
            if old_lesson and old_lesson in base and lesson:
                title = base.replace(old_lesson, lesson)
            else:
                title = f"{base}｜{lesson or '下一课时'}"
        new = self.create_project(
            title,
            p['subject'],
            grade=p['grade'],
            notes=p['notes'],
            owner=p['owner'],
            knowledge_module=module,
            lesson=lesson,
            learner_profile=p.get('learner_profile', ''),
        )
        with self.store.tx() as c:
            c.execute(
                'UPDATE projects SET prev_project_id=?, carryover=? WHERE id=?', (pid, _json(carry), new['id'])
            )  # 本课时内容 and the standard items are left empty: the next lesson covers new content
            for alias in roster:
                c.execute(
                    'INSERT INTO learners(id,project_id,alias,created_at) VALUES(?,?,?,?)',
                    (new_id('lrn'), new['id'], alias, time.time()),
                )
            event(c, pid, 'next_lesson_created', {'next_project': new['id']})
            event(c, new['id'], 'carried_over', {'from_project': pid, 'patterns': len(carry['patterns'])})
            return self._project_view(self._project(c, new['id']))

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
        lesson_scope=None,
        curriculum_ids=None,
        curriculum_off=None,
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
                ('lesson_scope', lesson_scope),
            ):
                if value is not None:
                    changes[key] = value.strip()[:500]
            if curriculum_ids is not None or curriculum_off is not None:
                prev = json.loads(p['curriculum']) if p.get('curriculum') else {}
                data = CUR.load(p['subject'])
                ids = prev.get('ids', []) if curriculum_ids is None else curriculum_ids
                ids = [i for i in dict.fromkeys(ids) if data and i in data['by_id']]
                off = prev.get('off', False) if curriculum_off is None else bool(curriculum_off)
                changes['curriculum'] = _json({'ids': ids, 'off': off, 'chosen_by': 'teacher'})
            if 'knowledge_module' in changes or 'lesson_scope' in changes:
                prev = json.loads(p['curriculum']) if p.get('curriculum') else {}
                if prev.get('chosen_by') == 'auto' and 'curriculum' not in changes:
                    changes['curriculum'] = ''  # the automatic pick follows the course info; a teacher's pick stays
            if subject is not None and pack(subject)['id'] != p['subject']:
                has_questions = c.execute(
                    "SELECT 1 FROM artifacts WHERE project_id=? AND type='question' LIMIT 1", (pid,)
                ).fetchone()
                if has_questions:
                    raise Conflict('项目里已经有题目，不能再改学科；请新建一个项目')
                changes['subject'] = pack(subject)['id']
                changes['curriculum'] = ''
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
