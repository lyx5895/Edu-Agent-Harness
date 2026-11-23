"""02 出题改题: generation, quality review and independent solving, A/B choice, paper import, 换一道."""

from __future__ import annotations

import json
import re
import threading
import time

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core import grading, progress
from edu_core.access import CURRENT_USER
from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import CURRENT_RUN, event, new_id, record_generation
from edu_core.subjects import KEYED_TYPES, QUESTION_TYPES, SUGGESTED_ONLY_TYPES
from edu_core.text import dump, sha
from edu_core.workflow.common import (
    Conflict,
    _json,
    _question_content,
    _rubric_from,
    normalize_plan,
    usable_drafts,
)
from edu_core.workflow.prompts import (
    AGREE_SYSTEM,
    GENERATE_SYSTEM,
    IMPORT_SYSTEM,
    INFER_GOALS_SYSTEM,
    QUALITY_SYSTEM,
    SOLVE_SYSTEM,
    VARIANTS,
)
from edu_core.workflow.runs import artifact_project, operation
from edu_core.workflow.schemas import (
    AgreeOutput,
    ImportedPaper,
    InferredGoals,
    QualityOutput,
    QuestionDraft,
    QuestionSet,
    SolveOutput,
)

AB_RULE_VERSION = 'ab-v2'
AB_UI_VERSION = 'ui-2026-11b'
REASON_TAGS = ('更贴合目标', '难度更合适', '更清楚', '有事实错误', '依据不足', '其他')


def _bigrams(text):
    text = re.sub(r'\s+', '', text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _mark_similar(items, threshold=0.6):
    """Parts are written without seeing each other: a question much like an earlier one gets a note that says so
    (never a 需核对 flag). Similarity is the overlap of character pairs of the stems, options included."""
    seen = []
    for k, (d, checks) in enumerate(items):
        grams = _bigrams(d.stem + ''.join(d.options))
        for j, other in seen:
            if grams and other and len(grams & other) / min(len(grams), len(other)) >= threshold:
                checks.setdefault('notes', []).append(f'和本次生成的第 {j + 1} 道题内容相近，可以换个角度')
                break
        seen.append((k, grams))


def _checks_failed(checks):
    """A version the teacher should not be asked to compare: a doubtful answer, a must-fix issue, or a check that
    could not run. Merely differing in difficulty is not a failure."""
    return bool(checks.get('answer_suspect') or checks.get('flags') or checks.get('solver') == 'error')


class QuestionsMixin:
    """02 出题改题."""

    # ----- questions -----

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

    @operation('generate_questions')
    def generate_questions(
        self, pid, *, types=None, count=2, difficulty='中等', notes='', objective_ids=None, ab=None, purpose='课堂练习'
    ):
        llm = self._model()
        with self.store.read() as c:
            plan = self._plan(c, pid)
            draft = None if plan else self._plan(c, pid, confirmed=False)
        if plan is None:
            if self._teacher_draft(draft):
                raise Conflict('备课方案还是草稿：先在“01 备课设计”确认，再按目标出题')
            # 出一份练习 without a lesson plan: objectives inferred from a paper or from the request
            plan = draft if draft and draft['content'].get('objectives') else self._infer_goals(pid, llm, notes)
        with self.store.read() as c:
            p = self._project(c, pid)
            objs = self._confirmed_objectives(c, pid, objective_ids, plan=plan)
            if not objs:
                raise Conflict('备课方案里没有选中的教学目标')
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            self._add_plan(ctx, plan)
            cited = sorted({i for o in objs for i in o['content'].get('standard_ids', [])})
            hints, standards = [], []
            if cited:
                std = CUR.context(p['subject'], cited, self._curriculum_query(p))
                hints = [it['text'] for it in std['hints']]
                standards = [{'位置': it['label'], '原文': it['text']} for it in std['content']]
                ctx.add_other('curriculum', p['subject'], sha(_json(cited)), items=cited)
            carry = self._carryover_for_prompt(c, ctx, p)
            materials = self._material_context(c, ctx, pid, limit=6000)
        allowed = [t for t in (types or p['pack']['types']) if t in p['pack']['types']]
        if not allowed:
            raise ValueError('所选题型不属于该学科')
        carry_part = {carry.pop('label'): carry} if carry else {}

        def request(part):
            """The generation input for some of the objectives (``part``: indexes into ``objs``). One part per
            objective keeps each reply short (a whole set used to run past max_tokens) and lets parts run side by
            side. Question types follow each objective's success criterion: rotating suggested types per objective
            pushed objectives that ask students to write or compute towards multiple choice."""
            sub = [objs[i] for i in part]
            return {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': [
                    {'index': k, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                    for k, o in enumerate(sub)
                ],
                '本课重点': plan['content'].get('key_points', []),
                '本课难点': plan['content'].get('difficulties', []),
                '本课时内容': p.get('lesson_scope', ''),
                '课标条目': standards,
                '课标教学提示': hints,
                # the other objectives are another part's: named so this part stays inside its own
                '其他目标': [objs[i]['content']['text'] for i in range(len(objs)) if i not in part],
                '课堂活动': [a for a in self._activities_for_prompt(plan, sub) if a['对应目标序号']],
                '备课备注': plan['content'].get('notes', ''),
                # one 巩固题 for the whole set, not one per part
                **(carry_part if part[0] == 0 else {}),
                '学生基础': p.get('learner_profile', ''),
                '材料摘录': materials,
                '允许题型': [QUESTION_TYPES[t] + f'（{t}）' for t in allowed],
                '每个目标出题数': max(1, min(int(count), 5)),
                '练习用途': purpose,
                '难度': difficulty,
                '老师说明': notes or p['notes'],
            }

        parts = [[i] for i in range(len(objs))]

        if ab is None:
            trigger, rate = 'sampled', self.settings.ab_rate
        else:
            trigger, rate = 'requested', 1.0 if ab else 0.0
        # One comparison per objective at most: the teacher reads a set, not a stack of A/B cards.
        sampled = {part[0] for part in parts if rate > 0 and self.rng.random() < rate}
        groups = {i: new_id('ab') for i in sampled}

        def generate(part, variant):
            system = GENERATE_SYSTEM.format(
                subject=p['pack']['name'],
                types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed),
                variant=VARIANTS[variant],
            )
            payload = request(part)
            if variant == 'b':
                # One independent draw for the objective; it is paired with one of this part's A questions.
                payload['每个目标出题数'] = 1
            out, meta = llm.complete(
                'generator', 'generate_questions', system, _json(payload), QuestionSet, max_tokens=12000
            )
            drafts, dropped = usable_drafts(out.questions, allowed)
            for d in drafts:  # the part numbered its objectives from 0: back to the plan's numbering
                d.objective_index = part[d.objective_index] if 0 <= d.objective_index < len(part) else part[0]
            # Checks happen once the questions are saved (``_finish_checks``): inline or in the background, they
            # no longer decide how long the teacher waits for the questions themselves.
            return meta, [(d, {'notes': []}) for d in drafts], dropped, payload

        def arm(part, variant):
            """One arm of one part. A failure is returned to the caller instead of taking the other arm down."""
            try:
                return generate(part, variant), None
            except Exception as e:  # noqa: BLE001 - one arm failing must not lose the other
                return None, f'第 {part[0] + 1} 条目标的题没有生成成功（{type(e).__name__}: {e}）'[:200]

        def run_part(part):
            """One part: A always; B as an independent draw when this objective is sampled. The two arms share the
            input and differ only in the system prompt, so they can run side by side."""
            if part[0] in sampled:
                a, b = self._parallel(lambda variant: arm(part, variant), ['a', 'b'])
                return {'objective': part[0], 'group': groups[part[0]], 'a': a, 'b': b}
            return {'objective': part[0], 'group': None, 'a': arm(part, 'a'), 'b': None}

        def attempt_part(part):
            try:
                return run_part(part), None
            except Exception as e:  # noqa: BLE001 - run_part itself failed before an arm reported
                return None, f'第 {part[0] + 1} 条目标的题没有生成成功（{type(e).__name__}: {e}）'[:200]

        progress.step('出题')
        done = self._parallel(attempt_part, parts)
        pieces = [r for r, _ in done if r is not None]
        failures = [err for _, err in done if err]
        all_pairs = []
        for piece in pieces:
            for arm_name in ('a', 'b'):
                result = piece.get(arm_name)
                if result and result[0]:
                    all_pairs.extend(result[0][1])
                elif result and result[1]:
                    failures.append(result[1])
        _mark_similar(all_pairs)
        dropped = [
            x
            for piece in pieces
            for arm_name in ('a', 'b')
            if piece.get(arm_name) and piece[arm_name][0]
            for x in piece[arm_name][0][2]
        ]
        dropped += failures

        result = {'context_note': describe(ctx.manifest), 'candidates': [], 'alternatives': []}
        job = {
            'pid': pid,
            'p': p,
            'objs': objs,
            'difficulty': difficulty,
            'purpose': purpose,
            'trigger': trigger,
            'rate': rate,
            'a_pairs': [],
            'b_pairs': [],
            'manifest': ctx.manifest,
            'ab_notes': [],
        }
        with self.store.tx() as c:
            gids, made, written = [], [], []
            for piece in pieces:
                a_result, _a_error = piece['a']
                if a_result is None:
                    continue
                meta, items, _dropped, a_payload = a_result
                group = piece['group']
                # one generation record per part; both arms share the comparison group when sampled
                gid = record_generation(
                    c,
                    pid,
                    meta,
                    ctx.manifest,
                    'generate-v5',
                    group=group,
                    position='A' if group else None,
                    variant='a',
                )
                gids.append(gid)
                a_questions = self._save_questions(
                    c,
                    pid,
                    items,
                    objs,
                    gid,
                    status='draft',
                    round_=1,
                    extra={'difficulty_requested': difficulty, 'purpose': purpose},
                )
                made += a_questions
                written += [d for d, _ in items]
                if not group:
                    continue
                b_result, b_error = piece['b']
                if b_result is None:
                    job['ab_notes'].append(b_error or '另一种写法没有生成成功')
                    continue
                meta_b, items_b, _dropped_b, _b_payload = b_result
                bgid = record_generation(
                    c,
                    pid,
                    meta_b,
                    ctx.manifest,
                    'generate-v5b',
                    group=group,
                    position='B',
                    variant='b',
                )
                paired_index = self.rng.randrange(len(a_questions)) if a_questions else None
                paired_a = a_questions[paired_index] if paired_index is not None else None
                b_questions = self._save_questions(
                    c,
                    pid,
                    items_b,
                    objs,
                    bgid,
                    status='candidate',
                    round_=1,
                    extra={
                        'difficulty_requested': difficulty,
                        'purpose': purpose,
                        'paired_with': paired_a['id'] if paired_a else '',
                    },
                )
                if b_questions and paired_a is not None:
                    job['b_pairs'].append(
                        {
                            'group': group,
                            'objective': piece['objective'],
                            'a': paired_a,
                            'b': b_questions[0],
                            'b_draft': items_b[0][0],
                            'paired_a_index': paired_index,
                            'a_generation': gid,
                            'b_generation': bgid,
                            'a_payload': a_payload,
                            'meta_a': meta,
                            'meta_b': meta_b,
                        }
                    )
                else:
                    for b_q in b_questions:
                        c.execute(
                            "UPDATE artifacts SET status='superseded' WHERE lineage_id=? AND status='candidate'",
                            (b_q['lineage_id'],),
                        )
                    job['ab_notes'].append(f'第 {piece["objective"] + 1} 条目标的另一种写法没有生成成功')
            if not gids:
                raise ModelUnavailable('；'.join(failures) or '没有生成可用的题目')
            result['candidates'].append(
                {
                    'position': 'A',
                    'variant': 'a',
                    'generation_id': gids[0],
                    'generation_ids': gids,
                    'questions': made,
                    'dropped': dropped,
                }
            )
            job['a_pairs'] = list(zip(made, written))

        if self.settings.background_checks and job['a_pairs']:
            result['background'] = {'run': self._start_background(pid, job), 'questions': len(job['a_pairs'])}
            result['ab_note'] = '；'.join(job['ab_notes'])
            return result
        rewritten, failed, comparisons = self._finish_checks(job)
        # The checks (and a rewrite they called for) are in the database now: answer with the current version.
        result['candidates'][0]['questions'] = self._fresh_questions(pid, made)
        result['alternatives'] = comparisons
        notes = [*job['ab_notes']]
        if rewritten:
            notes.append(f'{len(rewritten)} 道题发现问题后已自动重写一次')
        if failed:
            notes.append(f'{len(failed)} 道题自动重写失败，保留原题并标为需核对')
        result['ab_note'] = '；'.join(notes)
        return result

    # ----- what happens after the questions are written -----

    def _fresh_questions(self, pid, made):
        """The current version of each question that was just written (an automatic rewrite may have replaced it),
        with its rubric and stale flag, in the order they were written."""
        with self.store.read() as c:
            out = []
            for q in made:
                r = c.execute(
                    "SELECT id FROM artifacts WHERE lineage_id=? AND status IN ('draft','confirmed') "
                    'ORDER BY version DESC LIMIT 1',
                    (q['lineage_id'],),
                ).fetchone()
                a = A.get(c, r['id']) if r else A.get(c, q['id'])
                a['rubric'] = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                a['stale'] = A.is_stale(c, a['id'])
                out.append(a)
            return out

    def _finish_checks(self, job):
        """Self-check every version, then publish only the comparisons whose two versions both passed. Runs inline
        or in a background thread (``_start_background``)."""
        a_pairs = job.get('a_pairs', [])
        b_pairs = job.get('b_pairs', [])
        if not a_pairs and not b_pairs:
            return [], [], []
        progress.step('自检')
        progress.grow('独立解题与审核', len(a_pairs) + len(b_pairs))

        def check_one(item):
            arm, q, d = item
            try:
                checks = self._self_check(
                    job['p'], d, self._review_ctx(job['p'], job['objs'], d, job['difficulty'])
                )
            except Exception as e:  # noqa: BLE001 - one question's check must not cost the others
                checks = {
                    'notes': [f'自检没有完成（{type(e).__name__}: {e}）'[:200]],
                    'solver': 'error',
                    '_metas': [],
                }
            finally:
                progress.tick()
            return arm, q['id'], q['content_sha'], d, checks

        tasks = [('a', q, d) for q, d in a_pairs] + [('b', bp['b'], bp['b_draft']) for bp in b_pairs]
        rows = [(qid, sha_, d, checks) for _arm, qid, sha_, d, checks in self._parallel(check_one, tasks)]
        written = self._write_checks(job['pid'], rows)
        checks_by_qid = {qid: merged for qid, _d, merged, _sha in written}
        a_ids = {q['id'] for q, _ in a_pairs}
        paired_a_ids = {bp['a']['id'] for bp in b_pairs}
        rewritten, failed = self._auto_rewrite(
            job['pid'], [r for r in written if r[0] in a_ids], paired_a_ids
        )
        comparisons = self._finalize_comparisons(job, checks_by_qid)
        return rewritten, failed, comparisons

    def _write_checks(self, pid, rows):
        """Write review and solve results into the questions they were computed for. A question the teacher edited or
        confirmed while the check ran is left alone: those findings describe an older text. A candidate (the B side)
        is checked too, so a comparison only ever uses two verified versions."""
        rows = [r for r in rows if r]
        if not rows:
            return []
        written = []
        with self.store.tx() as c:
            for qid, expected_sha, d, checks in rows:
                a = c.execute('SELECT content, content_sha, status FROM artifacts WHERE id=?', (qid,)).fetchone()
                if a is None or a['status'] not in ('draft', 'candidate') or a['content_sha'] != expected_sha:
                    continue
                checks = dict(checks)
                metas = checks.pop('_metas', [])
                manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
                content = json.loads(a['content'])
                old = content.get('checks') or {}
                generation_ids = [*old.get('generation_ids', [])]
                for m in metas:
                    generation_ids.append(record_generation(c, pid, m, manifest, 'self-check-v2'))
                merged = {**old, **checks}
                merged['generation_ids'] = generation_ids
                merged['notes'] = [*old.get('notes', []), *checks.get('notes', [])]
                content['checks'] = merged
                new_sha = sha(content)
                c.execute(
                    'UPDATE artifacts SET content=?, content_sha=? WHERE id=?', (dump(content), new_sha, qid)
                )
                written.append((qid, d, merged, new_sha))
        return written

    def _auto_rewrite(self, pid, results, paired_a_ids=()):
        """A question the check found a real problem with (a reference that disagrees with an independent solution,
        or must-fix issues) is rewritten once with those findings as the remark; the rewrite checks itself again.
        A question taking part in a comparison is left alone: replacing one side under the teacher's feet is worse
        than leaving the findings on the version they pick. There is no second loop."""
        rewritten, failed = [], []
        for qid, _d, checks, expected_sha in results:
            if qid in paired_a_ids:
                continue
            if not (checks.get('answer_suspect') or checks.get('flags')):
                continue
            with self.store.read() as c:
                cur = c.execute('SELECT status, content_sha FROM artifacts WHERE id=?', (qid,)).fetchone()
            if cur is None or cur['status'] != 'draft' or cur['content_sha'] != expected_sha:
                continue
            try:
                out = self.revise_question(qid, self._rewrite_remark(checks))
                rewritten.append(out['question']['id'])
            except (ModelUnavailable, Conflict, ValueError) as e:  # the flagged original stays for the teacher
                failed.append(f'{type(e).__name__}: {e}'[:200])
        return rewritten, failed

    def _finalize_comparisons(self, job, checks_by_qid):
        """Publish a comparison only when both versions passed their checks; record the rest as not comparable so
        the research data still shows what was generated and why it was not shown."""
        out = []
        pid = job['pid']
        for bp in job.get('b_pairs', []):
            a, b = bp['a'], bp['b']
            a_checks = checks_by_qid.get(a['id'], {})
            b_checks = checks_by_qid.get(b['id'], {})
            if a['id'] not in checks_by_qid or b['id'] not in checks_by_qid:
                status, note = 'not_comparable', '有一份没有完成自检，未展示比较'
            elif _checks_failed(a_checks) or _checks_failed(b_checks):
                status, note = 'not_comparable', '有一份没有通过自检，未展示比较'
            elif b['content']['stem'].strip() == a['content']['stem'].strip():
                status, note = 'same', '另一种写法与原题几乎相同，未展示比较'
            else:
                status, note = 'shown', ''
            display = {
                'A': {'question': a['id'], 'generation': bp['a_generation']},
                'B': {'question': b['id'], 'generation': bp['b_generation']},
                'shown_first': self.rng.choice(('A', 'B')),
                'paired_a_index': bp['paired_a_index'],
                'b_status': status,
                'snapshot': {
                    'objective': bp.get('objective'),
                    'arms': {
                        'A': {'prompt_version': 'generate-v5', 'model': bp['meta_a'].get('model_requested')},
                        'B': {'prompt_version': 'generate-v5b', 'model': bp['meta_b'].get('model_requested')},
                    },
                },
            }
            common = {k: v for k, v in bp['a_payload'].items() if k != '每个目标出题数'}
            with self.store.tx() as c:
                c.execute(
                    'INSERT INTO comparisons(candidate_group,project_id,unit,input_sha,factor,display,trigger,'
                    'sample_rate,rule_version,ui_version,status,shown_first,paired_a_index,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        bp['group'],
                        pid,
                        'question',
                        sha(_json(common)),
                        'prompt_variant_independent',
                        dump(display),
                        job['trigger'],
                        job['rate'],
                        AB_RULE_VERSION,
                        AB_UI_VERSION,
                        status,
                        display['shown_first'],
                        bp['paired_a_index'],
                        time.time(),
                    ),
                )
                if status != 'shown':
                    c.execute(
                        "UPDATE artifacts SET status='superseded' WHERE lineage_id=? AND status='candidate'",
                        (b['lineage_id'],),
                    )
                    event(c, pid, 'ab_not_comparable', {'group': bp['group'], 'reason': status})
            if status == 'shown':
                out.append(
                    {
                        'group': bp['group'],
                        'question': a['id'],
                        'alternative': b['id'],
                        'paired_a_index': bp['paired_a_index'],
                        'shown_first': display['shown_first'],
                    }
                )
            else:
                job.setdefault('ab_notes', []).append(note)
        return out

    @staticmethod
    def _rewrite_remark(checks):
        parts = []
        if checks.get('answer_suspect'):
            parts.append('参考答案与独立解题得出的结论对不上：请核对题干数据，或改正参考答案')
        parts += [str(f) for f in checks.get('flags', []) if str(f).strip()][:3]
        return '；'.join(parts)[:500]

    def _start_background(self, pid, job):
        """Run ``_finish_checks`` in a thread of this process. The run row and the live progress entry are created
        before the request returns, so the page can follow the work from the first second."""
        rid = new_id('run')
        user = CURRENT_USER.get()
        with self.store.tx() as c:
            task = c.execute('SELECT id FROM tasks WHERE project_id=? LIMIT 1', (pid,)).fetchone()
            c.execute(
                'INSERT INTO runs(id,project_id,task_id,kind,status,user_id,started_at) VALUES(?,?,?,?,?,?,?)',
                (
                    rid,
                    pid,
                    task[0] if task else '',
                    'generate_checks',
                    'running',
                    user['id'] if user else '',
                    time.time(),
                ),
            )
        progress.begin(rid, pid, 'generate_checks')

        def target():
            token = CURRENT_RUN.set(rid)
            status, error = 'succeeded', ''
            try:
                self._finish_checks(job)
            except BaseException as e:  # noqa: BLE001 - recorded on the run; the thread has no caller to raise to
                status, error = 'failed', f'{type(e).__name__}: {e}'[:500]
            finally:
                progress.end(rid)
                CURRENT_RUN.reset(token)
                with self.store.tx() as c:
                    c.execute(
                        'UPDATE runs SET status=?, error=?, finished_at=? WHERE id=?',
                        (status, error, time.time(), rid),
                    )

        threading.Thread(target=target, daemon=True, name=f'edu-checks-{rid}').start()
        return rid

    @staticmethod
    def _add_plan(ctx, plan):
        """A confirmed plan goes into the context as such. Objectives inferred from a request or a paper are not
        confirmed: they are listed as what they are, never passed off as a confirmed plan."""
        if plan['status'] == 'confirmed':
            ctx.add_artifact(plan)
        else:
            ctx.add_other(
                'inferred_objectives',
                plan['id'],
                plan['content_sha'],
                lineage_id=plan['lineage_id'],
                version=plan['version'],
            )

    @staticmethod
    def _teacher_draft(plan):
        """An unconfirmed plan drafted in 01 (not objectives inferred from a paper or a request): it must be confirmed
        before questions are written against it."""
        objs = (plan or {}).get('content', {}).get('objectives') or []
        return bool(objs) and not all(o.get('inferred') for o in objs)

    def _infer_goals(self, pid, llm, notes=''):
        """Practice straight from the request: infer the objectives the questions will test, saved as a draft plan
        whose objectives are marked 由出题要求反推. The teacher checks them with the questions; nothing is confirmed
        on their behalf."""
        self._confirm_standards(pid, llm)
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('infer_objectives')
            ctx.add_other('project', pid)
            materials = self._material_context(c, ctx, pid, limit=6000)
            if not (p.get('knowledge_module') or p.get('lesson_scope') or materials or notes.strip()):
                raise Conflict('请先写下这次练习的主题或范围，或添加参考材料')
            current = self._plan(c, pid, confirmed=False)
        progress.step('反推教学目标')
        with self.store.tx() as c:
            std = self._curriculum_for_plan(c, p)
        if std:
            ids = [it['id'] for it in std['content']]
            ctx.add_other('curriculum', p['subject'], sha(_json(ids)), items=ids)
        out, meta = llm.complete(
            'generator',
            'infer_objectives',
            INFER_GOALS_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    '学科': p['pack']['name'],
                    '年级': p['grade'],
                    '主题': p.get('knowledge_module', ''),
                    '课时': p.get('lesson', ''),
                    '本课时内容': p.get('lesson_scope', ''),
                    '学生基础': p.get('learner_profile', ''),
                    '老师说明': notes or p.get('notes', ''),
                    '课标条目': [{'位置': it['label'], '原文': it['text']} for it in (std or {}).get('content', [])],
                    '材料摘录': materials,
                }
            ),
            InferredGoals,
        )
        goals = [
            {'text': o.text, 'success_criterion': o.success_criterion, 'inferred': True}
            for o in out.objectives[:4]
            if o.text.strip()
        ]
        if not goals:
            raise ModelUnavailable('没有得到可用的教学目标，请补充主题或范围后重试')
        base = current['content'] if current else {'key_points': [], 'difficulties': [], 'activities': [], 'notes': ''}
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'infer-goals-v1')
            return A.create(
                c,
                pid,
                'lesson_plan',
                normalize_plan({**base, 'objectives': goals}),
                origin='generated',
                generation_id=gid,
                lineage_id=current['lineage_id'] if current else None,
            )

    def _review_ctx(self, p, objs, d, difficulty=None, pattern=None):
        """What the quality review compares a question with: its objective, the lesson scope and cited standards."""
        obj = objs[d.objective_index]['content'] if 0 <= d.objective_index < len(objs) else None
        data = CUR.load(p['subject'])
        standards = [
            data['by_id'][i]['text'] for i in (obj or {}).get('standard_ids', []) if data and i in data['by_id']
        ]
        return {
            '教学目标': obj['text'] if obj else '',
            '达成标准': obj.get('success_criterion', '') if obj else '',
            '针对的问题': pattern or '',
            '本课时内容': p.get('lesson_scope', ''),
            '课标条目': standards,
            '学生基础': p.get('learner_profile', ''),
            '要求难度': difficulty or '',
        }

    def _quality(self, p, d: QuestionDraft, review):
        """Reviewer judgement: does the question test its objective, stay in scope, match the asked difficulty?"""
        out, meta = self.llm.complete(
            'reviewer',
            'review_question',
            QUALITY_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    **{k: v for k, v in review.items() if v},
                    '题型': QUESTION_TYPES[d.type],
                    '题目': d.stem,
                    '选项': [f'{"ABCDEFGH"[i]}. {o}' for i, o in enumerate(d.options[:8])],
                    '参考答案': d.reference,
                    '评分要点': [x.description for x in d.points],
                }
            ),
            QualityOutput,
        )
        flags = []
        if review.get('教学目标') and not out.tests_objective:
            flags.append(f'与目标不符：{out.objective_note or "没有考到这条目标"}')
        if not out.in_scope:
            flags.append(f'超出范围：{out.scope_note or "用到了本课时或课标以外的内容"}')
        flags += [x.strip() for x in out.issues if x.strip()][:3]
        # A difficulty off the asked one and improvement ideas are worth seeing, but they are not reasons to call a
        # question 需核对: every question used to carry ~4 flags, and the real errors drowned among them.
        asked = review.get('要求难度')
        off = asked in ('基础', '中等', '较难') and out.difficulty != asked
        return {'difficulty': out.difficulty, 'difficulty_asked': asked if off else '', 'flags': flags}, meta

    def _self_check(self, p, d: QuestionDraft, review=None):
        """Two checks for trust in the question — not a grading. A reviewer checks objective and scope; another model
        solves the question without the key, and its answer is compared with the reference (design doc 4 ②). The
        solver's work is not graded point by point: the teacher never reads that, and the only conclusion that
        matters here is whether the reference answer can be trusted."""
        subject = p['subject']
        checks = {'notes': [], '_metas': []}
        solvable = d.type not in SUGGESTED_ONLY_TYPES and self.llm is not None

        def review_part():
            if review is None or self.llm is None:
                return None
            try:
                return self._quality(p, d, review)
            except ModelUnavailable as e:
                return e

        def solve_part():
            if not solvable:
                return None
            try:
                return self._solve_and_compare(p, d, subject)
            except ModelUnavailable as e:
                return e

        # The review and the independent solution do not depend on each other: run them side by side, then merge
        # in the original order (review, numeric check, solution) so the notes read the same as before.
        reviewed, solved = self._parallel(lambda part: part(), [review_part, solve_part])
        if isinstance(reviewed, ModelUnavailable):
            checks['notes'].append(f'题目审核失败：{reviewed}')
        elif reviewed is not None:
            quality, qmeta = reviewed
            checks.update(quality)
            checks['_metas'].append(qmeta)
        for x in d.points:
            if x.check_kind == 'numeric' and x.check_expected.strip():
                from edu_core.tools import check_numeric

                ok, _ = check_numeric(d.reference, x.check_expected)
                if ok is False:
                    checks['notes'].append(f'参考答案的最终数值与核对值 {x.check_expected} 不一致')
                    checks['reference_mismatch'] = True
        if not solvable:
            checks['solver'] = 'skipped'
            return checks
        if isinstance(solved, ModelUnavailable):
            checks.update(solver='error', notes=[*checks['notes'], f'自检失败：{solved}'])
        else:
            agree, solution, note, metas = solved
            checks['_metas'] = [*checks['_metas'], *metas]
            checks.update(
                solver='agree' if agree else 'disagree',
                solver_answer=solution.answer[:2000],
                solver_final=solution.final_answer,
                solver_model=metas[0].get('model_returned'),
            )
            if not agree:
                detail = f'（{note.strip()}）' if note and note.strip() else ''
                checks['notes'].append(
                    f'AI 独立做了一遍，得出“{solution.final_answer.strip()[:60]}”，和参考答案不一致{detail}，请核对'
                )
            # The solver works the numbers anyway, so it also checks that the given ones can exist: the reviewer
            # read the formulas and missed entropies no real 8-sample split can have.
            if solution.data_issue.strip():
                checks['flags'] = [*checks.get('flags', []), f'数据问题：{solution.data_issue.strip()}']
        checks['answer_suspect'] = checks.get('solver') == 'disagree' or bool(checks.get('reference_mismatch'))
        return checks

    def _solve_and_compare(self, p, d: QuestionDraft, subject):
        """Another model solves the question without the key; the answer is compared with the reference. Keyed and
        fill-blank questions are compared by the server; anything else gets one short judgement, not a grading."""
        solve_ctx = Context('self_check_solve')
        solve_ctx.add_other('question_stem', sha(d.stem))
        solution, meta = self.llm.complete(
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
        metas = [meta]
        if d.type in KEYED_TYPES or d.type == 'fill_blank':
            decision = grading._keyed(
                {'type': d.type, 'answer_key': d.answer_key, 'accepted': d.accepted, 'options': d.options},
                solution.final_answer,
            )
            if decision is not None:
                return decision, solution, '', metas
        out, meta2 = self.llm.complete(
            'reviewer',
            'self_check_compare',
            AGREE_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    '题型': QUESTION_TYPES[d.type],
                    '题目': d.stem,
                    '参考答案': d.reference,
                    '独立解答': solution.answer[:2000],
                    '独立解答的最终答案': solution.final_answer,
                }
            ),
            AgreeOutput,
            max_tokens=800,
        )
        metas.append(meta2)
        return out.consistent, solution, out.note, metas

    def _save_questions(self, c, pid, items, objs, gid, *, status, round_, patterns=(), origin='generated', extra=None):
        made = []
        no = self._next_no(c, pid, round_)
        g = c.execute('SELECT context_manifest FROM generations WHERE id=?', (gid,)).fetchone() if gid else None
        basis = json.loads(g[0]) if g else []
        for d, checks in items:
            checks = dict(checks)
            metas = checks.pop('_metas', [])
            manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
            checks['generation_ids'] = [record_generation(c, pid, m, manifest, 'self-check-v2') for m in metas]
            obj = objs[d.objective_index] if 0 <= d.objective_index < len(objs) else None
            pattern = patterns[d.target_pattern_index] if 0 <= d.target_pattern_index < len(patterns) else None
            content = _question_content(
                d, no, round_, [obj['lineage_id']] if obj else [], [pattern['lineage_id']] if pattern else []
            )
            content['checks'] = checks
            content.update(extra or {})
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
            if status != 'candidate':
                self._guard_basis(c, pid, q['id'], basis)
            made.append(q)
            if status != 'candidate':
                no += 1
        return made

    def choose(
        self,
        pid,
        group,
        verdict,
        reason='',
        teacher='teacher',
        *,
        work_choice=None,
        preference='none',
        reason_tags=None,
        decision_ms=0,
        rater_role='teacher',
    ):
        """A/B feedback on one compared question (AB spec 3, 4), kept as separate facts instead of one win/loss label.

        ``verdict``: ``A`` (keep the original), ``B`` (adopt the alternative as the question's next version),
        ``both_ok`` (both acceptable; the original stays), ``neither``, ``skip`` (no label); ``same`` is the old
        “差不多” and means both acceptable with A taken to work on. ``work_choice`` is the side taken to work on (for
        ``both_ok``). ``preference`` is ``better`` only when the teacher said the adopted one fits this task better;
        ``work_only`` when it was just picked to edit; ``none`` otherwise. Only ``better`` becomes a preference pair
        in a training export. Either way the alternative stops being a separate candidate.
        """
        if verdict == 'same':
            verdict, work_choice = 'both_ok', work_choice or 'A'
        if verdict not in ('A', 'B', 'both_ok', 'neither', 'skip'):
            raise ValueError('选择应为 A、B、both_ok（两份都可用）、neither（都不合适）或 skip（暂不比较）')
        if preference not in ('better', 'work_only', 'none'):
            raise ValueError('偏好应为 better、work_only 或 none')
        if preference == 'better' and verdict not in ('A', 'B'):
            raise ValueError('只有采用 A 或 B 时才能表示“更适合本次任务”')
        chosen = verdict if verdict in ('A', 'B') else (work_choice if verdict == 'both_ok' else None)
        if chosen not in (None, 'A', 'B'):
            raise ValueError('继续修改的一份应为 A 或 B')
        acceptable = {'A': 'A', 'B': 'B', 'both_ok': 'both', 'neither': 'none', 'skip': ''}[verdict]
        tags = [t for t in (reason_tags or []) if t in REASON_TAGS]
        with self.store.tx() as c:
            cmp_row = c.execute(
                'SELECT display, status FROM comparisons WHERE candidate_group=? AND project_id=?', (group, pid)
            ).fetchone()
            if cmp_row is None:
                raise KeyError('候选组不存在')
            if cmp_row['status'] != 'shown':
                raise Conflict('这一组没有可比较的版本')
            display = json.loads(cmp_row['display'] or '{}')
            a_id = (display.get('A') or {}).get('question')
            b_id = (display.get('B') or {}).get('question')
            if not a_id or not b_id:
                raise Conflict('这一组不是同一道题的比较')
            if c.execute('SELECT 1 FROM choices WHERE candidate_group=? AND revoked_at IS NULL', (group,)).fetchone():
                raise Conflict('这一组已经选择过了；要改选请先撤回')
            cid = new_id('cho')
            c.execute(
                'INSERT INTO choices(id,project_id,candidate_group,verdict,chosen_position,reason,teacher,created_at,'
                'work_choice,preference,acceptable,reason_tags,decision_ms,rater_role) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    cid,
                    pid,
                    group,
                    verdict,
                    chosen,
                    reason,
                    teacher,
                    time.time(),
                    chosen or '',
                    preference,
                    acceptable,
                    dump(tags),
                    int(decision_ms or 0),
                    rater_role or 'teacher',
                ),
            )
            promoted = self._adopt_alternative(c, pid, a_id, b_id, group) if chosen == 'B' else []
            self._close_alternative(c, b_id)
            event(
                c,
                pid,
                'ab_choice',
                {'group': group, 'choice': cid, 'verdict': verdict, 'work_choice': chosen, 'preference': preference},
            )
            return {'choice_id': cid, 'verdict': verdict, 'promoted': promoted}

    def _adopt_alternative(self, c, pid, a_id, b_id, group=''):
        """B's text becomes the next version of A's question and rubric: one question with a history, not a second
        question for the same slot. B was generated independently, so the history records the comparison group and
        the version it was paired with, not "revised from A"."""
        base = A.get(c, a_id)
        alt = A.get(c, b_id)
        if base['lineage_id'] == alt['lineage_id']:
            return []
        base_rubric = self._rubric_for(c, base['lineage_id'], include_drafts=True)
        alt_rubric = self._rubric_for(c, alt['lineage_id'], include_drafts=True)
        content = {
            **alt['content'],
            'no': base['content'].get('no'),
            'adopted_from_group': group,
            'paired_with': a_id,
        }
        if base_rubric:
            content['restore_rubric'] = base_rubric['id']
        refs = [(r['ref_id'], r['role']) for r in base['refs']]
        q = A.create(
            c,
            pid,
            'question',
            content,
            origin='generated',
            refs=refs,
            generation_id=alt['generation_id'],
            lineage_id=base['lineage_id'],
            round_=base['round'],
        )
        made = [q['id']]
        if alt_rubric:
            q['rubric'] = A.create(
                c,
                pid,
                'rubric',
                {**alt_rubric['content'], 'question_lineage': q['lineage_id']},
                origin='generated',
                refs=[(q['id'], 'question'), *refs],
                generation_id=alt['generation_id'],
                lineage_id=base_rubric['lineage_id'] if base_rubric else None,
                round_=base['round'],
            )
            made.append(q['rubric']['id'])
        g = c.execute('SELECT context_manifest FROM generations WHERE id=?', (alt['generation_id'],)).fetchone()
        if g:
            self._guard_basis(c, pid, q['id'], json.loads(g['context_manifest'] or '[]'))
        return made

    @staticmethod
    def _close_alternative(c, b_id):
        """The alternative is no longer a candidate once its comparison has an outcome (kept as a research record
        in the comparison's display, not as a question in the list)."""
        b = A.get(c, b_id)
        c.execute(
            "UPDATE artifacts SET status='superseded' WHERE lineage_id=? AND status='candidate'", (b['lineage_id'],)
        )

    def revoke_choice(self, choice_id, teacher='teacher'):
        """Withdraw a choice (AB spec 4): a new event, the old row is kept and marked. If the choice adopted B, the
        question goes back to the version that was compared; the alternative becomes a candidate again, so the
        comparison can be made once more."""
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM choices WHERE id=?', (choice_id,)).fetchone()
            if r is None:
                raise KeyError('选择记录不存在')
            if r['revoked_at']:
                raise Conflict('这条选择已经撤回')
            c.execute('UPDATE choices SET revoked_at=? WHERE id=?', (time.time(), choice_id))
            cmp_row = c.execute(
                'SELECT display FROM comparisons WHERE candidate_group=?', (r['candidate_group'],)
            ).fetchone()
            display = json.loads(cmp_row['display'] or '{}') if cmp_row else {}
            a_id = (display.get('A') or {}).get('question')
            b_id = (display.get('B') or {}).get('question')
            restored = []
            if a_id:
                latest = c.execute(
                    'SELECT * FROM artifacts WHERE lineage_id=(SELECT lineage_id FROM artifacts WHERE id=?) '
                    'ORDER BY version DESC LIMIT 1',
                    (a_id,),
                ).fetchone()
                adopted = json.loads(latest['content']) if latest else {}
                # Only the version written by the adoption itself is rolled back; a later teacher edit is left alone.
                if (
                    latest
                    and latest['edit_of'] is None
                    and adopted.get('adopted_from_group') == r['candidate_group']
                ):
                    original = A.get(c, a_id)
                    content = {
                        k: v
                        for k, v in original['content'].items()
                        if k not in ('adopted_from_group', 'paired_with', 'restore_rubric')
                    }
                    content['no'] = adopted.get('no', content.get('no'))
                    content['restored_from'] = latest['id']
                    new_q = A.create(
                        c,
                        r['project_id'],
                        'question',
                        content,
                        origin='teacher',
                        refs=[(x['ref_id'], x['role']) for x in A.get(c, latest['id'])['refs']],
                        lineage_id=latest['lineage_id'],
                        round_=latest['round'],
                    )
                    restored.append(new_q['id'])
                    rubric_id = adopted.get('restore_rubric')
                    if rubric_id:
                        old_rubric = A.get(c, rubric_id)
                        new_rubric = A.create(
                            c,
                            r['project_id'],
                            'rubric',
                            {**old_rubric['content'], 'question_lineage': latest['lineage_id']},
                            origin='teacher',
                            refs=[(new_q['id'], 'question')],
                            lineage_id=old_rubric['lineage_id'],
                            round_=latest['round'],
                        )
                        restored.append(new_rubric['id'])
            if b_id:
                b = A.get(c, b_id)
                c.execute(
                    "UPDATE artifacts SET status='candidate' WHERE lineage_id=? AND status='superseded'",
                    (b['lineage_id'],),
                )
            event(
                c,
                r['project_id'],
                'ab_choice_revoked',
                {'choice': choice_id, 'teacher': teacher, 'restored': restored},
            )
        return {'ok': True}

    def merge_candidates(self, pid, group, question, rubric, sources, teacher='teacher'):
        """结合两份修改 (AB spec 3, 4): a new draft question whose sources are the original and its alternative. The
        final text has two sources and is never recorded as either of them."""
        with self.store.tx() as c:
            cmp_row = c.execute(
                'SELECT display FROM comparisons WHERE candidate_group=? AND project_id=?', (group, pid)
            ).fetchone()
            if cmp_row is None:
                raise Conflict('候选组不存在')
            display = json.loads(cmp_row['display'] or '{}')
            side = {(display.get(p) or {}).get('question'): p for p in ('A', 'B')}
            positions = {}
            for sid in sources:
                a = A.get(c, sid)
                if a['project_id'] != pid or sid not in side:
                    raise Conflict('来源不是这一组的原题或替代版本')
                positions[sid] = side[sid]
            if set(positions.values()) != {'A', 'B'}:
                raise ValueError('结合修改需要原题和替代版本各一份')
            q = self._validate_content('question', {**question, 'no': self._next_no(c, pid, 1)}, {})
            made = A.create(
                c,
                pid,
                'question',
                {**q, 'merged_from': sources},
                origin='merged',
                created_by=teacher,
                refs=[(s, 'merged_from') for s in sources],
            )
            r = self._validate_content('rubric', {**rubric, 'question_lineage': made['lineage_id']}, {})
            made['rubric'] = A.create(
                c, pid, 'rubric', r, origin='merged', created_by=teacher, refs=[(made['id'], 'question')]
            )
            if not c.execute(
                'SELECT 1 FROM choices WHERE candidate_group=? AND revoked_at IS NULL', (group,)
            ).fetchone():
                c.execute(
                    'INSERT INTO choices(id,project_id,candidate_group,verdict,chosen_position,reason,teacher,created_at,'
                    'work_choice,preference,acceptable,reason_tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (new_id('cho'), pid, group, 'merged', None, '', teacher, time.time(), '', 'none', '', '[]'),
                )
            b_id = (display.get('B') or {}).get('question')
            if b_id:
                self._close_alternative(c, b_id)
            event(c, pid, 'ab_merged', {'group': group, 'sources': sources, 'question': made['id']})
            return made

    @operation('import_paper')
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
        allowed = set(p['pack']['types'])
        drafts, dropped = usable_drafts(out.questions, allowed)
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
            keep = []
            for d in drafts:
                if 0 <= d.objective_index < len(objs):
                    keep.append(d)
                else:
                    dropped.append(f'没有对应教学目标：{d.stem[:30]}')
            checks = [{'notes': ['由导入试卷整理，评分要点和教学目标为反推，请核对'], 'imported': True} for _ in keep]
            made = self._save_questions(
                c, pid, list(zip(keep, checks)), objs, gid, status='draft', round_=1, origin='imported'
            )
        return {
            'lesson_plan': plan,
            'objectives': [o['content'] for o in objs],
            'questions': made,
            'dropped': dropped,
            'context_note': describe(ctx.manifest),
        }

    @staticmethod
    def _activities_for_prompt(plan, objs):
        """课堂活动 of the plan, with the objective numbers as the generator sees them (index into its goal list)."""
        index = {o['lineage_id']: i for i, o in enumerate(objs)}
        return [
            {
                '活动': a['name'],
                '用时': f'{a.get("minutes", 0)} 分钟',
                '做法': a.get('description', ''),
                '对应目标序号': [index[x] for x in a.get('objective_ids', []) if x in index],
            }
            for a in plan['content'].get('activities', [])
        ]

    def _carryover_for_prompt(self, c, ctx, p):
        """上一课时学情 or 本班学情: problem patterns with their re-practice result."""
        carry = self._prior_status(c, ctx, p)
        if not carry:
            return None
        return {
            'label': carry['label'],
            '学情概述': carry['summary'],
            '问题模式': [
                {
                    '名称': x['name'],
                    '描述': x['description'],
                    '涉及人数': x['affected'],
                    '再练情况': x['after_practice'],
                }
                for x in carry['patterns']
            ],
            **({'说明': carry['note']} if carry.get('note') else {}),
        }

    def coverage(self, pid, round_=1):
        """目标覆盖: how many questions each confirmed objective has in a round, and how many need a look."""
        with self.store.read() as c:
            plan = self._working_plan(c, pid)
            goals = self._goals(plan)
        qs = self.questions(pid, round_=round_)
        rows = []
        for g in goals:
            mine = [
                q for q in qs if g['lineage_id'] in q['content'].get('objective_ids', []) and q['status'] != 'candidate'
            ]
            flagged = [
                q['content']['no']
                for q in mine
                if q['content'].get('checks', {}).get('answer_suspect') or q['content'].get('checks', {}).get('flags')
            ]
            rows.append(
                {
                    'id': g['lineage_id'],
                    'text': g['content']['text'],
                    'success_criterion': g['content'].get('success_criterion', ''),
                    'total': len(mine),
                    'confirmed': sum(1 for q in mine if q['confirmed_version']),
                    'flagged': flagged,
                }
            )
        loose = [q['content']['no'] for q in qs if not q['content'].get('objective_ids') and q['status'] != 'candidate']
        # Objectives not yet confirmed (inferred from the request or a paper, or a plan draft) are shown as such.
        unconfirmed = bool(plan) and not plan.get('confirmed_version') and plan['status'] != 'confirmed'
        # Why the objectives are unconfirmed decides what the page offers: objectives inferred from a request or a
        # paper are corrected right here; a plan drafted in 01 is reviewed and confirmed in 01 before questions.
        draft_kind = ('teacher_draft' if self._teacher_draft(plan) else 'inferred') if unconfirmed else ''
        return {
            'round': round_,
            'objectives': rows,
            'unassigned': loose,
            'unconfirmed': unconfirmed,
            'draft_kind': draft_kind,
            'plan_id': plan['id'] if plan else None,
        }

    def adopt_solver_answer(self, qid, teacher='teacher'):
        """答案存疑 → take the independent solver's answer as a new draft version for the teacher to confirm. The
        rubric's old numeric/algebra expectations referred to the answer being replaced, so they are cleared too:
        otherwise grading would keep comparing student work with the value the teacher just rejected."""
        with self.store.tx() as c:
            q = A.get(c, qid)
            if q['type'] != 'question':
                raise ValueError('只能对题目操作')
            ck = dict(q['content'].get('checks') or {})
            if not ck.get('solver_answer'):
                raise Conflict('这道题没有独立解题的结果')
            content = dict(q['content'])
            if content['type'] in KEYED_TYPES or content['type'] == 'fill_blank':
                content['answer_key'] = ck.get('solver_final', '').strip()
            content['reference'] = ck['solver_answer']
            ck.update(
                answer_suspect=False,
                solver='adopted',
                notes=[*ck.get('notes', []), '已采用独立解题的答案；原先的数值核对值已清除，请核对评分要点'],
            )
            content['checks'] = ck
            new_q = A.edit(c, qid, content, teacher=teacher)
            rubric = self._rubric_for(c, q['lineage_id'], include_drafts=True)
            if rubric:
                points, removed = [], False
                for p in rubric['content']['points']:
                    p = dict(p)
                    if p.pop('check', None) is not None:
                        removed = True
                    points.append(p)
                if removed:
                    A.create(
                        c,
                        q['project_id'],
                        'rubric',
                        {**rubric['content'], 'points': points, 'question_lineage': q['lineage_id']},
                        origin='teacher',
                        refs=[(new_q['id'], 'question')],
                        lineage_id=rubric['lineage_id'],
                        round_=q['round'],
                    )
            return new_q

    @operation('revise_question', artifact_project)
    def revise_question(self, qid, instruction):
        """按意见修改: the same question changed only where the teacher's remark says (数据换成整数, 去掉第 2 问),
        checked like a new one and saved as its next version. Editing every field by hand was too much work."""
        if not (instruction or '').strip():
            raise ValueError('请写下修改意见')
        return self.regenerate_question(qid, instruction.strip(), keep=True)

    @operation('regenerate_question', artifact_project)
    def regenerate_question(self, qid, instruction='', difficulty=None, *, keep=False):
        """换一道: a new question for the same objective and type, saved as the next version of this question.
        With ``keep`` (按意见修改) the original is handed over whole and only what the remark asks is changed."""
        llm = self._model()
        with self.store.read() as c:
            q = A.get(c, qid)
            if q['type'] != 'question':
                raise ValueError('只能对题目操作')
            pid = q['project_id']
            p = self._project(c, pid)
            plan = self._working_plan(c, pid)
            objs = (
                self._confirmed_objectives(c, pid, q['content'].get('objective_ids') or None, plan=plan) if plan else []
            )
            objs = objs[:1]
            pattern = None
            for lineage in q['content'].get('target_patterns', []):
                pat = c.execute(
                    'SELECT content FROM artifacts WHERE lineage_id=? ORDER BY version DESC LIMIT 1', (lineage,)
                ).fetchone()
                if pat:
                    pc = json.loads(pat['content'])
                    pattern = f'{pc["name"]}：{pc["description"]}'
            old_rubric = self._rubric_for(c, q['lineage_id'], include_drafts=True)
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            if plan:
                self._add_plan(ctx, plan)
            cited = sorted({i for o in objs for i in o['content'].get('standard_ids', [])})
            standards, hints = [], []
            if cited:
                std = CUR.context(p['subject'], cited, self._curriculum_query(p))
                standards = [{'位置': it['label'], '原文': it['text']} for it in std['content']]
                hints = [it['text'] for it in std['hints']]
                ctx.add_other('curriculum', p['subject'], sha(_json(cited)), items=cited)
            materials = self._material_context(c, ctx, pid, limit=6000)
            activities = self._activities_for_prompt(plan, objs) if plan else []
        if not objs:
            raise Conflict('这道题没有可用的教学目标：请先在备课方案里确认目标，再换一道')
        difficulty = difficulty or q['content'].get('difficulty_requested') or '中等'
        qtype = q['content']['type']
        system = GENERATE_SYSTEM.format(
            subject=p['pack']['name'], types=f'{qtype}（{QUESTION_TYPES[qtype]}）', variant=VARIANTS['a']
        )
        if keep:
            system += '\n这次不是出新题：按“老师的修改意见”修改“原题”，意见没有提到的题干、选项、答案和评分要点都保持原样；修改后数据仍要自洽。'
        if keep:
            original = {
                '题干': q['content']['stem'],
                '选项': q['content'].get('options', []),
                '答案': q['content'].get('answer_key', ''),
                '参考答案': q['content'].get('reference', ''),
                '评分要点': [
                    {'要求': x.get('description', ''), '分值': x.get('score')}
                    for x in (old_rubric or {}).get('content', {}).get('points', [])
                ],
            }
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': [
                    {'index': 0, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                    for o in objs
                ],
                '针对的问题': pattern or '',
                '本课时内容': p.get('lesson_scope', ''),
                '课标条目': standards,
                '课标教学提示': hints,
                '本课重点': (plan or {}).get('content', {}).get('key_points', []),
                '本课难点': (plan or {}).get('content', {}).get('difficulties', []),
                '课堂活动': [a for a in activities if a['对应目标序号']],
                '材料摘录': materials,
                '学生基础': p.get('learner_profile', ''),
                '每个目标出题数': 1,
                '难度': difficulty,
                **(
                    {'原题': original, '老师的修改意见': instruction}
                    if keep
                    else {
                        '原题（请换一道，不要重复）': q['content']['stem'],
                        '老师的修改要求': instruction or '换一道考查同一目标的新题',
                    }
                ),
            }
        )
        task = 'revise_question' if keep else 'regenerate_question'
        out, meta = llm.complete('generator', task, system, user, QuestionSet)
        drafts, dropped = usable_drafts(out.questions, [qtype])
        if not drafts:
            raise ModelUnavailable('模型没有给出可用的题目，请重试' + (f'（{dropped[0]}）' if dropped else ''))
        d = drafts[0].model_copy(update={'objective_index': 0})
        checks = self._self_check(p, d, self._review_ctx(p, objs, d, difficulty, pattern))
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'revise-question-v1' if keep else 'regenerate-v1')
            metas = checks.pop('_metas', [])
            manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
            checks['generation_ids'] = [record_generation(c, pid, m, manifest, 'self-check-v2') for m in metas]
            content = _question_content(
                d,
                q['content']['no'],
                q['round'],
                q['content'].get('objective_ids', []),
                q['content'].get('target_patterns', []),
            )
            content.update(
                checks=checks,
                difficulty_requested=difficulty,
                **({'revised_from': qid} if keep else {'regenerated_from': qid}),
                instruction=instruction or '',
                purpose=q['content'].get('purpose', ''),
            )
            refs = [(r['ref_id'], r['role']) for r in A.get(c, qid)['refs']]
            new = A.create(
                c,
                pid,
                'question',
                content,
                origin='generated',
                refs=refs,
                generation_id=gid,
                lineage_id=q['lineage_id'],
                round_=q['round'],
            )
            rubric = _rubric_from(d.points, q['lineage_id'], (q['content'].get('target_patterns') or [None])[0])
            new['rubric'] = A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin='generated',
                refs=[(new['id'], 'question'), *refs],
                generation_id=gid,
                lineage_id=old_rubric['lineage_id'] if old_rubric else None,
                round_=q['round'],
            )
            self._guard_basis(c, pid, new['id'], ctx.manifest)
        return {'question': new, 'context_note': describe(ctx.manifest)}

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
            # by question number, so a question keeps its place after it is edited or regenerated
            return sorted(qs, key=lambda q: (q['round'], q['status'] == 'candidate', q['content'].get('no', 0)))
