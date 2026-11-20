"""Research records (COURSE_WORKSPACE_DESIGN.md 12; AB_AND_TRAINING_DATA.md 6–9).

* Counts keep teacher tasks, runs, internal model calls, artifact versions and research cases apart; retries and
  internal reviews never add cases. Contributor counts are reported only when real accounts exist.
* Source groups join everything that shares material, original questions, answers, rubrics or derivation, so a
  split never puts one source on both sides.
* Research cases are one per task stage; a formal case needs an independent review (not by anyone who produced
  it), with the standard and how disagreements were settled.
* Training export needs the course's training consent and never includes model inputs with student text: in the
  first version student answers, photos and inputs quoting them stay out entirely (AB spec 10).
"""

from __future__ import annotations

import hashlib
import json
import time

from edu_core import artifacts as A
from edu_core.access import CURRENT_USER, Forbidden, is_admin
from edu_core.storage import event, new_id, row
from edu_core.text import dump, sha
from edu_core.workflow.common import Conflict

# Model tasks whose input contains student answers, photos or quotes from them.
STUDENT_TASKS = {'grade', 'review', 'audit', 'analyze', 'transcribe', 'draft_review', 'practice', 'unit_review'}
STAGES = ('lesson_plan', 'questions', 'grading', 'analysis', 'review', 'explanation')


def _split_of(group, seed, test_ratio):
    h = int(hashlib.sha256(f'{seed}:{group}'.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 'test' if h < test_ratio else 'dev'


class ResearchMixin:
    """Research counts, source groups, research cases with independent review, consent and training export."""

    def _project_ids(self, c, course_id=None):
        if course_id:
            return [r[0] for r in c.execute('SELECT id FROM projects WHERE course_id=?', (course_id,))]
        return [r[0] for r in c.execute('SELECT id FROM projects')]

    # ----- source groups -----

    def _source_groups(self, c, pids):
        """artifact id -> source group id, by union-find over references, edits, lineages, material families and
        the materials each generation read."""
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        if not pids:
            return {}
        requested = set(pids)
        pids = self._project_ids(c)
        marks = ','.join('?' * len(pids))
        arts = [
            dict(r)
            for r in c.execute(
                f'SELECT id, project_id, lineage_id, edit_of, generation_id FROM artifacts WHERE project_id IN ({marks})',
                pids,
            )
        ]
        for a in arts:
            find('a:' + a['id'])
            union('a:' + a['id'], 'l:' + a['lineage_id'])
            if a['edit_of']:
                union('a:' + a['id'], 'a:' + a['edit_of'])
        for r in c.execute(
            f'SELECT r.artifact_id, r.ref_id FROM artifact_refs r JOIN artifacts a ON a.id=r.artifact_id WHERE a.project_id IN ({marks})',
            pids,
        ):
            union('a:' + r['artifact_id'], 'a:' + r['ref_id'])
        gens = {a['generation_id'] for a in arts if a['generation_id']}
        for g in gens:
            m = c.execute('SELECT context_manifest FROM generations WHERE id=?', (g,)).fetchone()
            for item in json.loads(m[0]) if m else []:
                if item.get('kind') == 'material' and item.get('family_id'):
                    for a in arts:
                        if a['generation_id'] == g:
                            union('a:' + a['id'], 'f:' + item['family_id'])
        groups = {}
        for a in arts:
            root = find('a:' + a['id'])
            if a['project_id'] not in requested:
                continue
            groups[a['id']] = 'sg_' + hashlib.sha256(root.encode()).hexdigest()[:12]
        return groups

    def source_groups(self, course_id=None):
        with self.store.read() as c:
            groups = self._source_groups(c, self._project_ids(c, course_id))
        sizes = {}
        for g in groups.values():
            sizes[g] = sizes.get(g, 0) + 1
        return {'groups': len(sizes), 'sizes': sizes, 'artifacts': groups}

    def check_split(self, assignment):
        """``assignment`` maps artifact or case ids to a split name. Returns the source groups found in more than one
        split, plus exact duplicates of question stems across groups (to be looked at, not merged automatically)."""
        with self.store.read() as c:
            groups = self._source_groups(c, self._project_ids(c))
            by_group = {}
            for key, split in assignment.items():
                ids = [key]
                case = c.execute('SELECT artifact_ids FROM research_cases WHERE id=?', (key,)).fetchone()
                if case:
                    ids = json.loads(case[0])
                for aid in ids:
                    if aid in groups:
                        by_group.setdefault(groups[aid], set()).add(split)
            stems = {}
            for r in c.execute("SELECT id, content FROM artifacts WHERE type='question'"):
                stem = json.loads(r['content']).get('stem', '').strip()
                if stem and r['id'] in groups:
                    stems.setdefault(sha(stem), set()).add(groups[r['id']])
        return {
            'crossing_groups': sorted(g for g, s in by_group.items() if len(s) > 1),
            'duplicate_stems_across_groups': [sorted(g) for g in stems.values() if len(g) > 1],
        }

    # ----- research cases and independent review -----

    def build_research_cases(self, course_id=None):
        """One case per task stage (and per assignment or round where it applies), from adopted versions only.
        Retries, candidates, internal reviews and edits fold into the same case. Re-running updates the artifact list;
        a reviewed case whose artifacts changed goes back to ``pending``."""
        made = 0
        with self.store.tx() as c:
            pids = self._project_ids(c, course_id)
            groups = self._source_groups(c, pids)
            for pid in pids:
                p = self._project(c, pid)
                task = c.execute('SELECT id FROM tasks WHERE project_id=? LIMIT 1', (pid,)).fetchone()
                for stage, scope, ids in self._case_units(c, pid):
                    if not ids:
                        continue
                    group = groups.get(ids[0], '')
                    old = c.execute(
                        'SELECT id, artifact_ids, status FROM research_cases WHERE project_id=? AND stage=? AND scope=?',
                        (pid, stage, scope),
                    ).fetchone()
                    if old is None:
                        c.execute(
                            'INSERT INTO research_cases(id,course_id,project_id,task_id,stage,scope,artifact_ids,source_group,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                            (
                                new_id('case'),
                                p.get('course_id', ''),
                                pid,
                                task[0] if task else '',
                                stage,
                                scope,
                                dump(sorted(ids)),
                                group,
                                time.time(),
                            ),
                        )
                        made += 1
                    elif json.loads(old['artifact_ids']) != sorted(ids):
                        c.execute(
                            "UPDATE research_cases SET artifact_ids=?, source_group=?, version=version+1, status=CASE WHEN status='pending' THEN 'pending' ELSE 'changed' END WHERE id=?",
                            (dump(sorted(ids)), group, old['id']),
                        )
        return {'created': made, **self.research_summary(course_id)}

    def _case_units(self, c, pid):
        def confirmed(type_, **kw):
            out = []
            for a in A.current(c, pid, type_, **kw):
                conf = A.confirmed_in_lineage(c, a['lineage_id']) if a['confirmed_version'] else None
                if conf:
                    out.append(conf['id'])
            return out

        units = [('lesson_plan', '', confirmed('lesson_plan')), ('explanation', '', confirmed('explanation'))]
        rounds = sorted(
            {
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT round FROM artifacts WHERE project_id=? AND type='question'", (pid,)
                )
            }
        )
        for rnd in rounds:
            units.append(('questions', f'round{rnd}', confirmed('question', round_=rnd)))
        scopes = [('', f'round{rnd}', rnd) for rnd in rounds]
        scopes += [
            (r['id'], r['id'], r['round'])
            for r in c.execute('SELECT id, round FROM assignments WHERE project_id=?', (pid,))
        ]
        for aid, scope, rnd in scopes:
            kw = {'assignment': aid, 'round_': rnd}
            units.append(('grading', scope, confirmed('grading', **kw)))
            units.append(('analysis', scope, confirmed('analysis', **kw) + confirmed('problem_pattern', **kw)))
            units.append(('review', scope, confirmed('review_plan', **kw)))
        if not rounds:
            units.append(('review', 'unit', confirmed('review_plan', assignment='')))
        return units

    def research_cases(self, course_id=None, status=None):
        with self.store.read() as c:
            sql, args = 'SELECT * FROM research_cases WHERE 1=1', []
            if course_id:
                sql, args = sql + ' AND course_id=?', [*args, course_id]
            if status:
                sql, args = sql + ' AND status=?', [*args, status]
            out = []
            for r in c.execute(sql + ' ORDER BY created_at', args).fetchall():
                r = row(r)
                r['artifact_ids'] = json.loads(r['artifact_ids'])
                r['reviews'] = [
                    dict(x)
                    for x in c.execute('SELECT * FROM case_reviews WHERE case_id=? ORDER BY created_at', (r['id'],))
                ]
                r['runs'] = c.execute('SELECT COUNT(*) FROM runs WHERE project_id=?', (r['project_id'],)).fetchone()[0]
                r['teacher_interventions'] = sum(
                    1
                    for aid in r['artifact_ids']
                    if A.get(c, aid)['origin'] in ('teacher', 'edited', 'revised', 'merged')
                )
                out.append(r)
            return out

    def review_case(self, case_id, reviewer, verdict, standard, note='', role='reviewer', expected_version=None):
        """独立审定: a reviewer who did not produce any of the case's artifacts records a verdict against a stated
        standard. Two reviewers who disagree make the case ``disputed`` until an ``adjudicator`` decides."""
        actor = CURRENT_USER.get()
        if self.settings.auth:
            if actor is None:
                raise Forbidden('请先登录再审定')
            if role == 'adjudicator' and not is_admin(actor):
                raise Forbidden('只有管理员可以复议')
            reviewer = actor['id']
        reviewer, standard = (reviewer or '').strip(), (standard or '').strip()
        if not reviewer or not standard:
            raise ValueError('请填写审定人和审定标准')
        if verdict not in ('approve', 'reject'):
            raise ValueError('审定结论应为 approve 或 reject')
        if role not in ('reviewer', 'adjudicator'):
            raise ValueError('角色应为 reviewer 或 adjudicator')
        with self.store.tx() as c:
            case = c.execute('SELECT * FROM research_cases WHERE id=?', (case_id,)).fetchone()
            if case is None:
                raise KeyError('研究案例不存在')
            if expected_version is not None and expected_version != case['version']:
                raise Conflict('案例内容已变化，请刷新后审定最新版本')
            producers = set()
            for aid in json.loads(case['artifact_ids']):
                artifact = A.get(c, aid)
                producers.update(
                    r[0]
                    for r in c.execute(
                        'SELECT DISTINCT created_by FROM artifacts WHERE lineage_id=?', (artifact['lineage_id'],)
                    )
                )
            producers |= {
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT user_id FROM runs WHERE project_id=? AND user_id!=''", (case['project_id'],)
                )
            }
            owner = c.execute('SELECT owner_id FROM projects WHERE id=?', (case['project_id'],)).fetchone()
            if owner and owner[0]:
                producers.add(owner[0])
            if reviewer in producers or reviewer == 'teacher':
                raise Conflict('审定人不能是产出这些内容的人（独立审定）')
            if c.execute(
                'SELECT 1 FROM case_reviews WHERE case_id=? AND reviewer=? AND case_version=?',
                (case_id, reviewer, case['version']),
            ).fetchone():
                raise Conflict('这位审定人已经审定过这个案例')
            c.execute(
                'INSERT INTO case_reviews(id,case_id,reviewer,role,verdict,standard,note,created_at,case_version) VALUES(?,?,?,?,?,?,?,?,?)',
                (new_id('rev'), case_id, reviewer, role, verdict, standard, note, time.time(), case['version']),
            )
            reviews = c.execute(
                'SELECT role, verdict FROM case_reviews WHERE case_id=? AND case_version=? ORDER BY created_at, rowid',
                (case_id, case['version']),
            ).fetchall()
            final = [r['verdict'] for r in reviews if r['role'] == 'adjudicator']
            verdicts = {r['verdict'] for r in reviews if r['role'] == 'reviewer'}
            if final:
                status = 'approved' if final[-1] == 'approve' else 'rejected'
            elif len(verdicts) > 1:
                status = 'disputed'
            else:
                status = 'approved' if verdicts == {'approve'} else 'rejected'
            c.execute('UPDATE research_cases SET status=? WHERE id=?', (status, case_id))
            event(
                c,
                case['project_id'],
                'case_reviewed',
                {'case': case_id, 'reviewer': reviewer, 'verdict': verdict, 'role': role},
            )
            return {'status': status}

    def _freeze_sources(self, c, artifact_ids, seed='v1', test_ratio=0.2, *, tuning=False):
        """Persist partitions on artifact identities, not a recomputed union-find root.

        Growing/merging source families inherits their previous partition. Conflicting
        historical partitions fail closed, including exports from before this registry.
        """
        groups = self._source_groups(c, self._project_ids(c))
        selected = {groups[aid] for aid in artifact_ids if aid in groups}
        members = {}
        for aid, group in groups.items():
            members.setdefault(group, []).append(aid)
        splits = {g: set() for g in selected}
        tuned = set()

        def remember(ids, split='', used=False):
            for aid in ids:
                group = groups.get(aid)
                if group not in selected:
                    continue
                if split:
                    splits[group].add(split)
                if used:
                    tuned.add(group)

        for r in c.execute('SELECT * FROM research_partitions'):
            remember([r['artifact_id']], r['split'], bool(r['tuned']))
        cases = [dict(r) for r in c.execute('SELECT * FROM research_cases')]
        case_ids = {r['id']: json.loads(r['artifact_ids']) for r in cases}
        for r in cases:
            remember(case_ids[r['id']], r['split'])
        # Preserve history made by the previous implementation during migration.
        for r in c.execute("SELECT payload FROM events WHERE kind='case_used_for_tuning'"):
            remember(case_ids.get(json.loads(r[0]).get('case'), []), used=True)
        for r in c.execute('SELECT manifest FROM dataset_exports'):
            for record in json.loads(r[0]).get('records', []):
                remember(record.get('origin_ids', []), record.get('split', ''))
        if tuning:
            tuned |= selected
        result = {}
        for group in selected:
            previous = splits[group]
            if len(previous) > 1 or ('test' in previous and group in tuned):
                raise Conflict('同源材料存在测试集与开发/调优历史冲突，请先整理历史划分；不能继续导出或调优')
            split = (
                next(iter(previous)) if previous else ('dev' if group in tuned else _split_of(group, seed, test_ratio))
            )
            for aid in members[group]:
                c.execute(
                    'INSERT OR REPLACE INTO research_partitions(artifact_id,split,tuned) VALUES(?,?,?)',
                    (aid, split, int(group in tuned)),
                )
                result[aid] = split
        for case in cases:
            ids = case_ids[case['id']]
            values = {result[aid] for aid in ids if aid in result}
            if len(values) > 1:
                raise Conflict('案例包含不同分区的来源，请拆分案例后再冻结')
            if values:
                c.execute('UPDATE research_cases SET split=? WHERE id=?', (next(iter(values)), case['id']))
        return result

    def freeze_split(self, course_id=None, test_ratio=0.2, seed='v1'):
        """Freeze once for the whole source family, including existing tuning history."""
        with self.store.tx() as c:
            sql, args = 'SELECT artifact_ids, split FROM research_cases', []
            if course_id:
                sql, args = sql + ' WHERE course_id=?', [course_id]
            cases = c.execute(sql, args).fetchall()
            self._freeze_sources(c, [aid for r in cases for aid in json.loads(r['artifact_ids'])], seed, test_ratio)
            n = sum(not r['split'] for r in cases)
        return {'frozen': n}

    def use_for_tuning(self, case_ids, purpose):
        """Reserve every connected source as dev before recording tuning use."""
        with self.store.tx() as c:
            ids = []
            for cid in case_ids:
                r = c.execute('SELECT artifact_ids FROM research_cases WHERE id=?', (cid,)).fetchone()
                if r is None:
                    raise KeyError(f'研究案例不存在：{cid}')
                ids.extend(json.loads(r['artifact_ids']))
            self._freeze_sources(c, ids, tuning=True)
            for cid in case_ids:
                event(c, 'research', 'case_used_for_tuning', {'case': cid, 'purpose': purpose})
        return {'ok': True}

    # ----- counts -----

    def research_summary(self, course_id=None):
        with self.store.read() as c:
            pids = self._project_ids(c, course_id)
            if not pids:
                return {'counts': {}, 'definitions': {}}
            marks = ','.join('?' * len(pids))
            runs = c.execute(
                f'SELECT kind, status, COUNT(*) n FROM runs WHERE project_id IN ({marks}) GROUP BY kind, status', pids
            ).fetchall()
            users = c.execute(
                f"SELECT COUNT(DISTINCT user_id) FROM runs WHERE project_id IN ({marks}) AND user_id!=''", pids
            ).fetchone()[0]
            accounts = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            counts = {
                'teacher_tasks': c.execute(
                    f'SELECT COUNT(*) FROM tasks WHERE project_id IN ({marks})', pids
                ).fetchone()[0],
                'runs': sum(r['n'] for r in runs),
                'runs_by_kind': {f'{r["kind"]}:{r["status"]}': r['n'] for r in runs},
                'model_calls': c.execute(
                    f'SELECT COUNT(*) FROM generations WHERE project_id IN ({marks})', pids
                ).fetchone()[0],
                'artifact_versions': c.execute(
                    f'SELECT COUNT(*) FROM artifacts WHERE project_id IN ({marks})', pids
                ).fetchone()[0],
                'research_cases': c.execute(
                    f'SELECT COUNT(*) FROM research_cases WHERE project_id IN ({marks})', pids
                ).fetchone()[0],
                'reviewed_cases': c.execute(
                    f"SELECT COUNT(*) FROM research_cases WHERE project_id IN ({marks}) AND status IN ('approved','rejected')",
                    pids,
                ).fetchone()[0],
                'source_groups': len(set(self._source_groups(c, pids).values())),
                'courses': c.execute(
                    f"SELECT COUNT(DISTINCT course_id) FROM projects WHERE id IN ({marks}) AND course_id!=''", pids
                ).fetchone()[0],
                'contributors': users if accounts else None,
            }
        return {
            'counts': counts,
            'definitions': {
                'teacher_tasks': '老师开始的一项工作（备课、出练习、批改、讲解），旧项目整理后各算一项',
                'runs': '老师发起的一次模型操作（生成、批改一次布置、分析……），失败和重试各算一次运行',
                'model_calls': '所有模型调用，包括自检、盲审、抽检和单份重试；不代表任务或案例数',
                'research_cases': '每项任务的每个阶段（按布置或轮次）一个案例；候选、重试、内部复核和编辑不增加案例',
                'source_groups': '共享材料、原题、评分依据或互相派生的一组记录，切分时整体处理',
                'contributors': '只有启用账号时才统计；未设置真实身份时为空，不推算教师人数',
            },
        }

    # ----- consent and training export -----

    def grant_consent(self, course_id, purposes, version, granted_by):
        purposes = sorted({p for p in purposes if p in ('training', 'evaluation', 'research')})
        if not purposes:
            raise ValueError('请选择允许的用途：training、evaluation 或 research')
        with self.store.tx() as c:
            self._course(c, course_id)
            cid = new_id('cons')
            c.execute(
                'INSERT INTO consents(id,course_id,purposes,version,granted_by,granted_at) VALUES(?,?,?,?,?,?)',
                (cid, course_id, dump(purposes), str(version)[:40], str(granted_by)[:60], time.time()),
            )
            event(c, course_id, 'consent_granted', {'consent': cid, 'purposes': purposes, 'version': version})
            return {'id': cid, 'purposes': purposes, 'version': version}

    def consents(self, course_id):
        with self.store.read() as c:
            return [
                {**dict(r), 'purposes': json.loads(r['purposes'])}
                for r in c.execute('SELECT * FROM consents WHERE course_id=? ORDER BY granted_at DESC', (course_id,))
            ]

    def revoke_consent(self, consent_id):
        """Stops later exports and marks every dataset that already contains records of that course. It cannot undo
        what a trained model already learned (AB spec 9)."""
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM consents WHERE id=?', (consent_id,)).fetchone()
            if r is None:
                raise KeyError('授权记录不存在')
            c.execute('UPDATE consents SET revoked_at=? WHERE id=?', (time.time(), consent_id))
            affected = []
            for d in c.execute('SELECT id, manifest FROM dataset_exports').fetchall():
                m = json.loads(d['manifest'])
                ids = [x['record_id'] for x in m.get('records', []) if x.get('course_id') == r['course_id']]
                if ids:
                    m.setdefault('revocations', []).append({'consent': consent_id, 'at': time.time(), 'records': ids})
                    c.execute('UPDATE dataset_exports SET manifest=? WHERE id=?', (dump(m), d['id']))
                    affected.append(d['id'])
            event(c, r['course_id'], 'consent_revoked', {'consent': consent_id, 'datasets': affected})
        return {'datasets_marked': affected}

    def _active_consent(self, c, course_id):
        for r in c.execute(
            'SELECT * FROM consents WHERE course_id=? AND revoked_at IS NULL ORDER BY granted_at DESC', (course_id,)
        ):
            if 'training' in json.loads(r['purposes']):
                return dict(r)
        return None

    def export_dataset(self, course_id=None, seed='v1', test_ratio=0.2):
        """A versioned training dataset (AB spec 6–9). Demonstrations (task input → teacher's final adopted version),
        explicit preferences only (``better``), and edits with a real teacher instruction. Records come only from
        courses with an active training consent; any input containing student text is excluded, and every exclusion is
        counted with its reason. Inputs are what the model saw; final versions and labels never go into inputs."""
        records, excluded = [], {}

        def skip(reason):
            excluded[reason] = excluded.get(reason, 0) + 1

        with self.store.tx() as c:
            pids = self._project_ids(c, course_id)
            groups = self._source_groups(c, pids)
            consents = {}
            for pid in pids:
                p = self._project(c, pid)
                cid = p.get('course_id', '')
                if cid not in consents:
                    consents[cid] = self._active_consent(c, cid) if cid else None
                consent = consents[cid]
                for a in A.listing(c, pid):
                    if (
                        not a['generation_id']
                        or a['version'] != 1
                        or a['status'] == 'candidate'
                        or a['type'] == 'rubric'
                    ):
                        continue  # a rubric goes into its question's record
                    g = row(c.execute('SELECT * FROM generations WHERE id=?', (a['generation_id'],)).fetchone())
                    if g is None:
                        continue
                    if g['task'] in STUDENT_TASKS or any(m.get('has_answer') for m in g['context_manifest'] or []):
                        skip('student_text')
                        continue
                    final = A.confirmed_in_lineage(c, a['lineage_id'])
                    if final is None:
                        skip('not_adopted')
                        continue
                    if consent is None:
                        skip('no_training_consent')
                        continue
                    records.append(
                        self._record(
                            'demonstration',
                            a,
                            g,
                            consent,
                            groups,
                            cid,
                            target=self._demo_target(c, final),
                            label='teacher_adopted',
                            final_id=final['id'],
                        )
                    )
                for a in A.listing(c, pid):
                    instr = (a['content'].get('revision') or {}).get('instruction') or a['content'].get('instruction')
                    if (
                        a['origin'] not in ('revised', 'generated')
                        or not instr
                        or (not a['edit_of'] and not a['content'].get('regenerated_from'))
                    ):
                        continue
                    final = A.confirmed_in_lineage(c, a['lineage_id'])
                    if final is None or final['version'] < a['version'] or A.is_stale(c, final['id']):
                        skip('not_adopted')
                        continue
                    src = A.get(c, a['edit_of'] or a['content']['regenerated_from'])
                    if consent is None:
                        skip('no_training_consent')
                        continue
                    records.append(
                        {
                            **self._record(
                                'edit',
                                a,
                                None,
                                consent,
                                groups,
                                cid,
                                target=final['content'],
                                label='teacher_instruction',
                                final_id=final['id'],
                            ),
                            'input': {'original': src['content'], 'instruction': instr},
                        }
                    )
                for ch in c.execute(
                    'SELECT * FROM choices WHERE project_id=? AND revoked_at IS NULL', (pid,)
                ).fetchall():
                    if ch['preference'] != 'better':
                        skip('no_explicit_preference' if ch['verdict'] in ('A', 'B') else f'label_{ch["verdict"]}')
                        continue
                    if consent is None:
                        skip('no_training_consent')
                        continue
                    gens = {
                        r['position']: row(r)
                        for r in c.execute(
                            'SELECT * FROM generations WHERE candidate_group=?', (ch['candidate_group'],)
                        )
                    }
                    other = 'B' if ch['chosen_position'] == 'A' else 'A'
                    if ch['chosen_position'] not in gens or other not in gens:
                        skip('incomplete_comparison')
                        continue

                    by_gen = {}
                    for x in A.listing(c, pid, 'question'):
                        if x['version'] == 1:  # the original candidates, never a later edit
                            by_gen.setdefault(x['generation_id'], []).append(x)
                    chosen_qs = by_gen.get(gens[ch['chosen_position']]['id'], [])
                    if not chosen_qs:
                        skip('incomplete_comparison')
                        continue
                    first = chosen_qs[0]
                    records.append(
                        {
                            **self._record(
                                'preference',
                                first,
                                gens[ch['chosen_position']],
                                consent,
                                groups,
                                cid,
                                target=None,
                                label='teacher_explicit_preference',
                                final_id='',
                            ),
                            'chosen': [x['content'] for x in chosen_qs],
                            'rejected': [x['content'] for x in by_gen.get(gens[other]['id'], [])],
                            'comparison': ch['candidate_group'],
                        }
                    )
            skip_student = (
                c.execute(
                    f"SELECT COUNT(*) FROM artifacts WHERE type IN ('grading','analysis','problem_pattern') AND project_id IN ({','.join('?' * len(pids)) or 'NULL'})",
                    pids,
                ).fetchone()[0]
                if pids
                else 0
            )
            if skip_student:
                excluded['verification_student_text'] = skip_student
            partitions = self._freeze_sources(c, [r['origin_ids'][0] for r in records], seed, test_ratio)
            for r in records:
                r['split'] = partitions[r['origin_ids'][0]]
            records.sort(key=lambda r: r['record_id'])
            version = 'ds_' + sha(dump(records))[:12]
            manifest = {
                'version': version,
                'created_at': time.time(),
                'counts': {
                    t: sum(1 for r in records if r['type'] == t) for t in ('demonstration', 'preference', 'edit')
                },
                'excluded': excluded,
                'source_groups': len({r['source_group'] for r in records}),
                'split': {s: sum(1 for r in records if r['split'] == s) for s in ('dev', 'test')},
                'deidentification': '不含学生原文、照片或引用学生原文的输入；老师材料未做自动去标识检查，导出前请人工抽查',
                'records': [
                    {
                        k: r[k]
                        for k in (
                            'record_id',
                            'type',
                            'course_id',
                            'source_group',
                            'consent',
                            'origin_ids',
                            'split',
                            'label',
                        )
                    }
                    for r in records
                ],
            }
            c.execute(
                'INSERT INTO dataset_exports(id,version,manifest,created_at) VALUES(?,?,?,?)',
                (new_id('ds'), version, dump(manifest), time.time()),
            )
        return {'manifest': manifest, 'records': records}

    def _demo_target(self, c, final):
        """The teacher's adopted version; a question carries its adopted rubric in the same record."""
        if final['type'] != 'question':
            return final['content']
        rubric = self._rubric_for(c, final['lineage_id'])
        return {'question': final['content'], 'rubric': rubric['content'] if rubric else None}

    @staticmethod
    def _record(type_, artifact, generation, consent, groups, course_id, *, target, label, final_id):
        return {
            'record_id': sha(f'{type_}:{artifact["id"]}:{final_id}')[:16],
            'type': type_,
            'course_id': course_id,
            'task': generation['task'] if generation else artifact['type'],
            'input': generation['request'] if generation else None,
            'target': target,
            'label': label,
            'source_group': groups.get(artifact['id'], ''),
            'consent': {'id': consent['id'], 'version': consent['version']},
            'origin_ids': [x for x in (artifact['id'], final_id, generation['id'] if generation else '') if x],
            'prompt_version': generation['prompt_version'] if generation else '',
            'model': generation['model_returned'] if generation else '',
        }
