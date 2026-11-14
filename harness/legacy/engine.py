import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from harness.errors import Conflict
from harness.grading.algebra import verify
from harness.legacy import runtime
from harness.legacy.course import ITEMS, SKILLS, public_item, retrieve
from harness.legacy.policy import VERSION, decide
from harness.llm.model import ModelClient
from harness.paths import ROOT
from harness.persistence.storage import SQLITE_BUSY_TIMEOUT
from harness.resources.images import normalize_image


class Engine:
    def __init__(self, db=None, model=None):
        self.db = str(db or ROOT / 'data/learning.sqlite')
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        self.model = model or ModelClient()
        self.lock = threading.RLock()
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, learner TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                learner TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(session TEXT, request_id TEXT, digest TEXT, response TEXT,
                PRIMARY KEY(session, request_id));
            """)

    def connect(self):
        c = sqlite3.connect(self.db, timeout=SQLITE_BUSY_TIMEOUT)
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def transaction(self):
        with self.lock:
            c = self.connect()
            try:
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def event(self, c, session, learner, kind, payload):
        cursor = c.execute(
            'INSERT INTO events(session,learner,type,payload,at) VALUES(?,?,?,?,?)',
            (session, learner, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        return cursor.lastrowid

    def create(self, learner, skill='k01'):
        if skill not in SKILLS:
            raise ValueError('未知知识点')
        if not learner.strip() or len(learner) > 80:
            raise ValueError('请输入有效演示身份')
        sid = uuid.uuid4().hex
        state = {
            'item': f'{skill}-teach-1',
            'hint_level': 0,
            'failures': 0,
            'done': False,
            'pending_image': None,
            'version': 0,
            'needs_review': False,
            'check_seen': [],
            'practice_round': 1,
        }
        with self.transaction() as c:
            c.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, learner, json.dumps(state)))
        return self.session(sid)

    def _load(self, c, sid):
        row = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        if not row:
            raise ValueError('会话不存在')
        state = json.loads(row['state'])
        state.setdefault('tutor', {'hypotheses': [], 'pending': None, 'assisted': False, 'outcome': 'unknown'})
        return row['learner'], state

    def _save(self, c, sid, state):
        state['version'] += 1
        c.execute('UPDATE sessions SET state=? WHERE id=?', (json.dumps(state, ensure_ascii=False), sid))

    def _view(self, sid, learner, s):
        public = {k: v for k, v in s.items() if k != 'tutor'}
        return {
            'session_id': sid,
            'learner': learner,
            **public,
            'pending_question': s.get('tutor', {}).get('pending'),
            'item': public_item(ITEMS[s['item']]),
        }

    def session(self, sid):
        with self.connect() as c:
            learner, s = self._load(c, sid)
            result = self._view(sid, learner, s)
            # Student history deliberately excludes diagnosis/reference/rubric fields.
            result['history'] = []
            for row in c.execute('SELECT id,type,payload,at FROM events WHERE session=? ORDER BY id', (sid,)):
                p = json.loads(row['payload'])
                entry = {'id': row['id'], 'type': row['type'], 'at': row['at']}
                entry.update({k: p[k] for k in ('text', 'feedback', 'hint', 'status', 'level', 'confirmed') if k in p})
                result['history'].append(entry)
            return result

    def operation(self, sid, request_id, kind, body, version=None):
        if not request_id or len(request_id) > 100:
            raise ValueError('需要有效request_id')
        digest = hashlib.sha256(
            json.dumps([kind, body, version], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.transaction() as c:
            old = c.execute('SELECT * FROM requests WHERE session=? AND request_id=?', (sid, request_id)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Conflict('同一request_id不能提交不同内容')
                return json.loads(old['response'])
            learner, s = self._load(c, sid)
            if version is not None and s['version'] != version:
                raise Conflict('页面状态已更新，请刷新后重试')
            if s['needs_review']:
                raise Conflict('教师已修正判定，请新建该知识点会话重新检查')
            if s['done']:
                raise Conflict('本次检查已结束，请选择新练习或复习')
            item = ITEMS[s['item']]
            ctx = self._context(c, learner, item)
            trace = None
            call_started_at = time.time()
            started = time.monotonic()
            if kind == 'image':
                if s['pending_image']:
                    raise Conflict('请先确认当前图片转写')
                url = self.validate_image(body['image_url'])
                transcript = self.model.transcribe(url)
                event = self.event(c, sid, learner, 'image', {'image_url': url, 'transcription': transcript})
                s['pending_image'] = {'event_id': event, **transcript}
                result = {
                    'transcription': s['pending_image'],
                    'feedback': '请确认或修正转写，仅保留你实际写出的解题步骤，再提交判定。',
                }
            elif kind == 'hint':
                if s['pending_image']:
                    raise Conflict('请先确认图片转写')
                if item['phase'] == 'check':
                    raise Conflict('这是独立检查，提交后再查看反馈；需要帮助可开始新的教学练习')
                s['hint_level'] = min(3, s['hint_level'] + 1)
                obs = {'kind': 'hint', 'status': 'unknown', 'phase': item['phase'], 'level': s['hint_level']}
                output, trace = runtime.execute(self.model, item, obs, s['tutor'], ctx)
                s['tutor']['assisted'] = True
                s['tutor']['pending'] = output if trace['decision']['action'] == 'concept_hint' else None
                result = {'hint': output, 'level': s['hint_level']}
                result['event_id'] = self.event(c, sid, learner, 'hint', {**result, 'item_id': item['id']})
            elif kind == 'reply':
                if s['pending_image']:
                    raise Conflict('请先确认图片转写')
                if item['phase'] == 'check':
                    raise Conflict('独立检查期间只能提交作答')
                if not s['tutor']['pending']:
                    raise Conflict('没有待回答的教学追问，请提交数学作答')
                text = body['text'].strip()
                if not text or len(text) > 3000:
                    raise ValueError('回答需为1至3000个字符')
                interpretation = self.model.interpret_reply(
                    {
                        'question': s['tutor']['pending'],
                        'student_reply': text,
                        'rule': SKILLS[item['skill']]['rule'],
                        'history': ctx,
                    }
                )
                # Independently enforce provenance even for custom adapters.
                quote = interpretation.get('evidence_quote', '')
                if not quote or quote not in text:
                    raise ValueError('诊断证据必须来自当前学生回答')
                s['tutor']['outcome'] = interpretation['outcome']
                s['tutor']['hypotheses'] = (
                    s['tutor']['hypotheses']
                    + [
                        {
                            **interpretation,
                            'basis': 'student_quote_model_interpretation',
                            'certainty': 'tentative',
                            'skill': item['skill'],
                        }
                    ]
                )[-3:]
                obs = {'kind': kind, 'status': 'unknown', 'phase': item['phase'], 'outcome': interpretation['outcome']}
                output, trace = runtime.execute(self.model, item, obs, s['tutor'], {**ctx, 'student_reply': text[:600]})
                trace['tools'].append('evidence.validate_quote')
                trace['interpretation'] = interpretation
                s['tutor']['assisted'] = True
                s['tutor']['pending'] = (
                    output if trace['decision']['action'] in ('clarify', 'prerequisite_review') else None
                )
                eid = self.event(
                    c,
                    sid,
                    learner,
                    'dialogue',
                    {
                        'item_id': item['id'],
                        'skill': item['skill'],
                        'text': text,
                        'feedback': output,
                        'interpretation': interpretation,
                        'evidence': 'dialogue_only',
                    },
                )
                s['tutor']['hypotheses'][-1]['event_id'] = eid
                result = {'event_id': eid, 'feedback': output, 'evidence': 'dialogue_only'}
            elif kind in ('attempt', 'confirm'):
                if kind == 'attempt' and s['pending_image']:
                    raise Conflict('请确认或修正图片转写后再提交')
                if kind == 'confirm' and not s['pending_image']:
                    raise Conflict('没有待确认的图片')
                text = body['text'].strip()
                if not text or len(text) > 3000:
                    raise ValueError('作答需为1至3000个字符')
                check = verify(item, text)
                diagnosis = self.model.diagnose(
                    item, text, check, {**retrieve(item['skill']), 'history': ctx, 'learner_state': s['tutor']}
                )
                status, source = check['status'], check['source']
                if status == 'unknown':
                    status = diagnosis['status'] if diagnosis['confidence'] == 'high' else 'unknown'
                    source = 'model_rubric'
                # This curriculum requires an algebraic result: self-reports cannot earn credit.
                if check['status'] == 'unknown' and status == 'correct':
                    status = 'unknown'
                if status == 'incorrect':
                    s['tutor']['hypotheses'] = (
                        s['tutor']['hypotheses']
                        + [
                            {
                                'hypothesis': diagnosis.get('hypothesis', '')[:160],
                                'basis': 'model_diagnosis',
                                'certainty': 'tentative',
                                'skill': item['skill'],
                                'evidence_quote': text[:160],
                            }
                        ]
                    )[-3:]
                assisted_before = s['tutor']['assisted']
                before = dict(s)
                feedback = check['reason'] if source == 'sympy' else '已按文字评分规则检查。'
                evidence = 'attempted'
                if status == 'correct':
                    evidence = (
                        'independent_check_passed'
                        if item['phase'] == 'check'
                        else ('assisted_complete' if s['hint_level'] or s['tutor']['assisted'] else 'practice_complete')
                    )
                    if item['phase'] == 'teach':
                        # Reserve checks across all sessions of this learner, not only this tab.
                        for row in c.execute('SELECT state FROM sessions WHERE learner=?', (learner,)):
                            s['check_seen'] = list(
                                set(s['check_seen']) | set(json.loads(row['state']).get('check_seen', []))
                            )
                        available = [
                            f'{item["skill"]}-check-{i}'
                            for i in (1, 2)
                            if f'{item["skill"]}-check-{i}' not in s['check_seen']
                        ]
                        if available:
                            s['item'] = available[0]
                            s['check_seen'].append(available[0])
                            feedback = '这道练习已完成。接下来做一题无提示检查。'
                        else:
                            s['done'] = True
                            feedback = '本会话可用的新检查题已用完，记录保留为练习证据。'
                        s['hint_level'], s['failures'] = 0, 0
                    else:
                        s['done'] = True
                        feedback = '本次独立检查通过，已保存原始作答。以后仍需复习验证。'
                elif status in ('incorrect', 'incomplete'):
                    if item['phase'] == 'check':
                        feedback = '本次独立检查未通过。回到教学练习，稍后使用另一道题检查。'
                        s['practice_round'] = min(3, s['practice_round'] + 1)
                        s['item'] = f'{item["skill"]}-teach-{s["practice_round"]}'
                        s['hint_level'], s['failures'] = 0, 0
                    else:
                        s['failures'] += 1
                        # Policy controls the intervention; the counter is only a bounded intensity signal.
                        if s['failures'] % 2 == 0:
                            s['hint_level'] = min(3, s['hint_level'] + 1)
                else:
                    feedback = '目前无法可靠判定，请补充清晰步骤。'
                obs = {
                    'kind': kind,
                    'status': status,
                    'phase': item['phase'],
                    'failures': s['failures'],
                    'outcome': s['tutor']['outcome'],
                }
                if item['phase'] == 'teach' and status != 'correct':
                    feedback, trace = runtime.execute(
                        self.model,
                        item,
                        obs,
                        s['tutor'],
                        {**ctx, 'student_work': text[:600], 'verification': check},
                        diagnosis,
                    )
                    trace['tools'].append('algebra.verify')
                    s['tutor']['assisted'] = True
                    s['tutor']['pending'] = feedback
                else:
                    trace = {
                        'policy_version': VERSION,
                        'input': obs,
                        'decision': decide(obs),
                        'tools': ['algebra.verify'],
                        'verification': check,
                        'output': feedback,
                        'wait_for_student': not s['done'],
                        'validation': {'accepted': True, 'scope': 'server_transition'},
                    }
                    s['tutor']['pending'] = None
                    s['tutor']['assisted'] = False
                    s['tutor']['outcome'] = 'unknown'
                payload = {
                    'item_id': item['id'],
                    'skill': item['skill'],
                    'phase': item['phase'],
                    'text': text,
                    'status': status,
                    'source': source,
                    'diagnosis': diagnosis,
                    'verification': check,
                    'hint_level': before['hint_level'],
                    'assisted': bool(assisted_before or before['hint_level']),
                    'evidence': evidence,
                    'feedback': feedback,
                    'confirmed': kind == 'confirm',
                    'image_event_id': s['pending_image']['event_id'] if s['pending_image'] else None,
                }
                event_id = self.event(c, sid, learner, 'attempt', payload)
                if status == 'incorrect' and s['tutor']['hypotheses']:
                    s['tutor']['hypotheses'][-1]['event_id'] = event_id
                s['pending_image'] = None
                result = {'event_id': event_id, 'status': status, 'evidence': evidence, 'feedback': feedback}
            else:
                raise ValueError('未知操作')
            if trace is not None:
                trace['item_id'] = item['id']
                trace['seconds'] = time.monotonic() - started
                trace['model_calls'] = [
                    record for record in getattr(self.model, 'calls', []) if record['at'] >= call_started_at
                ]
                trace['execution_budget'] = {'max_model_calls': 4, 'max_repairs_per_call': 1}
                trace['state_item_after'] = s['item']
                trace['state_version_before'] = s['version']
                trace['state_version_after'] = s['version'] + 1
                trace['evidence_event_id'] = result.get('event_id')
                trace['learner_state_after'] = json.loads(json.dumps(s['tutor']))
                trace_id = self.event(c, sid, learner, 'teaching_trace', trace)
                result['teaching_action'] = trace['decision']['action']
                result['trace_id'] = trace_id
            self._save(c, sid, s)
            result['session'] = self._view(sid, learner, s)
            c.execute(
                'INSERT INTO requests VALUES(?,?,?,?)',
                (sid, request_id, digest, json.dumps(result, ensure_ascii=False)),
            )
            return result

    def _context(self, c, learner, item):
        rows = c.execute('SELECT id,type,payload FROM events WHERE learner=? ORDER BY id DESC', (learner,)).fetchall()
        corrected = {json.loads(r['payload'])['event_id'] for r in rows if r['type'] == 'correction'}
        invalid_sessions = {
            r['id']
            for r in c.execute('SELECT id,state FROM sessions WHERE learner=?', (learner,))
            if json.loads(r['state']).get('needs_review')
        }
        invalid_ids = {
            r['id'] for sid in invalid_sessions for r in c.execute('SELECT id FROM events WHERE session=?', (sid,))
        }
        evidence = []
        for row in rows:
            if row['type'] not in ('attempt', 'dialogue', 'hint') or row['id'] in corrected | invalid_ids:
                continue
            payload = json.loads(row['payload'])
            old_item = ITEMS.get(payload.get('item_id'), {})
            if old_item.get('skill') != item['skill']:
                continue
            # Independent checks never receive previous tutoring or check answers.
            if item['phase'] == 'check':
                continue
            evidence.append({'id': row['id'], 'type': row['type'], 'payload': payload})
            if len(evidence) == 6:
                break
        return runtime.context(list(reversed(evidence)), item)

    def traces(self, sid):
        with self.connect() as c:
            self._load(c, sid)
            return [
                {'event_id': r['id'], **json.loads(r['payload'])}
                for r in c.execute(
                    "SELECT id,payload FROM events WHERE session=? AND type='teaching_trace' ORDER BY id", (sid,)
                )
            ]

    def replay(self, sid):
        return runtime.replay(self.traces(sid))

    @staticmethod
    def validate_image(url):
        return normalize_image(url)

    def report(self, learner=None):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM events ORDER BY id').fetchall()
            corrections = {}
            for row in rows:
                if row['type'] == 'correction':
                    p = json.loads(row['payload'])
                    corrections[p['event_id']] = p
            attempts = []
            for row in rows:
                if row['type'] != 'attempt' or (learner and row['learner'] != learner):
                    continue
                p = json.loads(row['payload'])
                p.update({'id': row['id'], 'learner': row['learner'], 'session_id': row['session'], 'at': row['at']})
                p['original_status'] = p['status']
                p['original_evidence'] = p['evidence']
                p['correction'] = corrections.get(row['id'])
                if p['correction']:
                    p['status'] = p['correction']['status']
                    p['source'] = 'teacher'
                    p['evidence'] = 'attempted'
                    if p['status'] == 'correct':
                        p['evidence'] = (
                            'independent_check_passed'
                            if p['phase'] == 'check' and not p['hint_level']
                            else ('assisted_complete' if p['hint_level'] or p.get('assisted') else 'practice_complete')
                        )
                attempts.append(p)
            summary = []
            for name in sorted({a['learner'] for a in attempts}):
                for skill in SKILLS:
                    group = [a for a in attempts if a['learner'] == name and a['skill'] == skill]
                    if not group:
                        continue
                    last_check = next((a for a in reversed(group) if a['phase'] == 'check'), None)
                    passed = bool(last_check and last_check['status'] == 'correct' and last_check['hint_level'] == 0)
                    summary.append(
                        {
                            'learner': name,
                            'skill': skill,
                            'name': SKILLS[skill]['name'],
                            'attempts': len(group),
                            'errors': sum(a['status'] == 'incorrect' for a in group),
                            'assisted_attempts': sum(bool(a['hint_level'] or a.get('assisted')) for a in group),
                            'independent_check_passed': passed,
                            'due_review': not passed or time.time() - last_check['at'] >= 86400,
                            'evidence_ids': [a['id'] for a in group],
                        }
                    )
            return {
                'summary': summary,
                'attempts': attempts,
                'revisions': [
                    dict(r) | {'payload': json.loads(r['payload'])}
                    for r in rows
                    if r['type'] == 'correction' and (not learner or r['learner'] == learner)
                ],
            }

    def progress(self, learner):
        report = self.report(learner)
        with self.connect() as c:
            sessions = [
                {'session_id': r['id'], 'skill': ITEMS[json.loads(r['state'])['item']]['skill']}
                for r in c.execute('SELECT * FROM sessions WHERE learner=?', (learner,))
            ]
        return {'learner': learner, 'summary': report['summary'], 'sessions': sessions}

    def correct(self, event_id, status, reason, request_id):
        if status not in ('correct', 'incorrect', 'incomplete', 'unknown') or not reason.strip():
            raise ValueError('请填写有效判定和修正理由')
        if not request_id or len(request_id) > 100:
            raise ValueError('需要request_id')
        body = {'event_id': event_id, 'status': status, 'reason': reason[:1000]}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.transaction() as c:
            old = c.execute(
                'SELECT * FROM requests WHERE session=? AND request_id=?', ('teacher', request_id)
            ).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Conflict('request_id冲突')
                return json.loads(old['response'])
            row = c.execute('SELECT * FROM events WHERE id=? AND type=?', (event_id, 'attempt')).fetchone()
            if not row:
                raise ValueError('作答记录不存在')
            learner, s = self._load(c, row['session'])
            revision = self.event(c, row['session'], learner, 'correction', body)
            s['needs_review'] = True
            self._save(c, row['session'], s)
            result = {'revision_id': revision, 'event_id': event_id, 'status': status}
            c.execute('INSERT INTO requests VALUES(?,?,?,?)', ('teacher', request_id, digest, json.dumps(result)))
            return result
