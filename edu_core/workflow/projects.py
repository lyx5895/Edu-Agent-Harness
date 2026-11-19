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
                'affected': f'{len(ct["affected_learners"])}/{ct["total_learners"]}',
            }
            t = trend.get(x['lineage_id'])
            if t and t['enough_evidence']:
                row_['after_practice'] = (
                    f'再练后仍有 {len(t["persisting"])} 人出错' if t['persisting'] else '再练后已改善'
                )
            else:
                row_['after_practice'] = '还没有再练结果'
            patterns.append(row_)
        # Only a confirmed analysis carries its summary forward (design doc 7.1, 7.2).
        analyses = [
            A.confirmed_in_lineage(c, a['lineage_id']) for a in A.current(c, pid, 'analysis') if a['confirmed_version']
        ]
        analyses = [a for a in analyses if a]
        reviews = [r for r in A.current(c, pid, 'review_plan') if r['confirmed_version']]
        plan = self._plan(c, pid)
        snapshot = {
            'from_project': pid,
            'from_title': p['title'],
            'from_lesson': p.get('lesson', ''),
            'summary': analyses[-1]['content'].get('summary', '') if analyses else '',
            'note': '部分分析：' + analyses[-1]['content']['coverage']['label']
            if analyses and analyses[-1]['content'].get('partial')
            else '',
            'patterns': patterns,
            'review_focus': A.confirmed_in_lineage(c, reviews[-1]['lineage_id'])['content'].get('focus', [])
            if reviews
            else [],
            'objectives': [g['content']['text'] for g in self._goals(plan)],
        }
        if not (snapshot['patterns'] or snapshot['summary']):
            raise Conflict('这一课时还没有已确认的问题模式或学情分析，先在“学情分析”中确认再进入下一课时')
        return snapshot

    def _prior_status(self, c, ctx, p):
        """上一课时学情 (a lesson made by 进入下一课时) or 本班学情 (a task of a class, design doc 7.1).

        The class status is computed when the model is called and registered in the manifest with the exact
        analysis and pattern versions used, so later revisions do not change what this run read.
        """
        if p.get('carryover'):
            carry = json.loads(p['carryover'])
            ctx.add_other('carryover', carry['from_project'], sha(p['carryover']))
            return carry | {'label': '上一课时学情'}
        if p.get('offering_id'):
            prefs = json.loads(p['status_prefs']) if p.get('status_prefs') else {}
            carry = self._class_status(c, p['offering_id'], prefs)
            if carry:
                ctx.add_other(
                    'class_status',
                    p['offering_id'],
                    sha(_json(carry)),
                    analyses=carry['analysis_ids'],
                    patterns=carry['pattern_ids'],
                    partial=bool(carry.get('note')),
                )
                return carry | {'label': '本班学情'}
        return None

    def next_lesson(self, pid, *, knowledge_module=None, lesson=None, title=None):
        """进入下一课时: a new project that inherits subject, grade, learner profile and roster, and carries this
        lesson's 学情 into its 备课方案."""
        with self.store.read() as c:
            p = self._project(c, pid)
            carry = self._carryover(c, pid)
            roster = [
                r['alias']
                for r in c.execute('SELECT alias FROM learners WHERE project_id=? ORDER BY created_at', (pid,))
            ]
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
                title = f'{base}｜{lesson or "下一课时"}'
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

    def add_material(self, pid, text, filename='粘贴文本', kind='text', family_of=None, course_id=None):
        """A material of one project (task), or with ``course_id`` and no project a shared course material."""
        text = (text or '').strip()
        if not text:
            raise ValueError('材料内容为空')
        if len(text) > 200_000:
            raise ValueError('材料过长（上限 20 万字）')
        with self.store.tx() as c:
            if pid:
                self._project(c, pid)
                course_id = ''
            else:
                self._course(c, course_id)
                pid = ''
            family = family_of or new_id('fam')
            mid = new_id('mat')
            c.execute(
                'INSERT INTO materials(id,project_id,family_id,kind,filename,sha256,text,created_at,course_id) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (mid, pid, family, kind, filename, sha(text), text, time.time(), course_id),
            )
            event(
                c, pid or course_id, 'material_added', {'material_id': mid, 'family_id': family, 'filename': filename}
            )
            return {
                'id': mid,
                'family_id': family,
                'filename': filename,
                'chars': len(text),
                'scope': 'project' if pid else 'course',
            }

    def _material_rows(self, c, pid):
        """The project's own materials, then the shared materials of its course."""
        rows = [
            dict(r) | {'scope': 'project'}
            for r in c.execute('SELECT * FROM materials WHERE project_id=? ORDER BY created_at', (pid,))
        ]
        course = c.execute('SELECT course_id FROM projects WHERE id=?', (pid,)).fetchone()
        if course and course[0]:
            rows += [
                dict(r) | {'scope': 'course'}
                for r in c.execute(
                    "SELECT * FROM materials WHERE course_id=? AND project_id='' ORDER BY created_at", (course[0],)
                )
            ]
        return rows

    def materials(self, pid):
        with self.store.read() as c:
            return [{**r, 'chars': len(r['text']), 'text': r['text'][:400]} for r in self._material_rows(c, pid)]

    def _material_context(self, c, ctx, pid, limit=8000):
        """Materials for a prompt. What was cut short, left out by the teacher or not sent at all is recorded in the
        manifest, so “已参考” says so instead of implying the model read everything (design doc 4.3). With a focus
        (章节或关键词) only the matching sections are sent, and their positions are recorded."""
        p = c.execute('SELECT material_prefs FROM projects WHERE id=?', (pid,)).fetchone()
        prefs = json.loads(p[0]) if p and p[0] else {}
        excluded, focus = set(prefs.get('exclude') or []), prefs.get('focus') or {}
        out, used = [], 0
        for r in self._material_rows(c, pid):
            extra = {
                'family_id': r['family_id'],
                'filename': r['filename'],
                'scope': r['scope'],
                'chars': len(r['text']),
            }
            if r['id'] in excluded:
                ctx.add_other('material', r['id'], r['sha256'], **extra, sent=0, excluded=True)
                continue
            text, segments = r['text'], None
            if focus.get(r['id']):
                segments = focus_segments(r['text'], focus[r['id']])
                text = '\n\n'.join(r['text'][a:b] for a, b in segments)
            piece = text[: max(0, limit - used)]
            used += len(piece)
            info = {**extra, 'sent': len(piece)}
            if segments is not None:
                info.update(focus=focus[r['id']], segments=segments, selected=len(text))
            ctx.add_other('material', r['id'], r['sha256'], **info)
            if piece:
                out.append({'文件': r['filename'], '内容': piece})
        return out

    def set_material_prefs(self, pid, exclude=None, focus=None):
        """排除材料、指定章节 (design doc 4.3) for this task. ``focus`` maps a material id to 章节名或关键词."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            known = {r['id'] for r in self._material_rows(c, pid)}
            prefs = json.loads(p['material_prefs']) if p.get('material_prefs') else {}
            if exclude is not None:
                prefs['exclude'] = sorted(set(exclude) & known)
            if focus is not None:
                prefs['focus'] = {k: str(v).strip()[:80] for k, v in focus.items() if k in known and str(v).strip()}
            c.execute('UPDATE projects SET material_prefs=? WHERE id=?', (json.dumps(prefs, ensure_ascii=False), pid))
            event(c, pid, 'material_prefs_updated', prefs)
            return prefs


HEADING = re.compile(r'^(#{1,6}\s|第[一二三四五六七八九十百\d]+[章节课]|[一二三四五六七八九十]+、|\d+(\.\d+)*\s)')


def focus_segments(text, query):
    """Character spans of the sections or paragraphs that match ``query`` (章节名或关键词, space separated).

    A heading line that matches takes its whole section, up to the next heading; otherwise a paragraph matches when
    it contains any term. Returns [] when nothing matches (the material is then not sent, and the manifest says so).
    """
    terms = [t for t in re.split(r'[\s,，、]+', query or '') if t]
    if not terms:
        return [(0, len(text))]
    lines, pos = [], 0
    for line in text.splitlines(keepends=True):
        lines.append((pos, pos + len(line), line))
        pos += len(line)
    spans, i = [], 0
    while i < len(lines):
        start, _end, line = lines[i]
        if HEADING.match(line.strip()) and any(t in line for t in terms):
            j = i + 1
            while j < len(lines) and not HEADING.match(lines[j][2].strip()):
                j += 1
            spans.append((start, lines[j - 1][1]))
            i = j
            continue
        i += 1
    if spans:
        return spans
    for m in re.finditer(r'[^\n]+(?:\n(?!\s*\n)[^\n]+)*', text):
        if any(t in m.group(0) for t in terms):
            spans.append((m.start(), m.end()))
    return spans
