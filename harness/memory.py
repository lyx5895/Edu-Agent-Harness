"""Evidence-derived memory. No model writes or uncalibrated mastery scores."""
import hashlib
import json
import time


def goal_key(state):
    task = state.get('original') or state.get('task') or {}
    scope = state.get('scope', {})
    identity = [scope.get('class_id', 'personal'), scope.get('course_id', 'personal'),
                scope.get('concept_id') or [task.get('domain'), task.get('objective')]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def summarize(c, learner, scope=None, exclude=None, now=None):
    now = time.time() if now is None else now
    groups = {}
    for row in c.execute('SELECT * FROM learning_sessions WHERE learner=? ORDER BY rowid', (learner,)):
        s = json.loads(row['state'])
        if row['id'] == exclude or not s.get('original'):
            continue
        if scope and any(s.get('scope', {}).get(k, 'personal') != v for k, v in scope.items()):
            continue
        key = goal_key(s)
        g = groups.setdefault(key, {'goal_id': key, 'objective': s['original']['objective'],
            'scope': s.get('scope', {}), 'source_sessions': [], 'evidence': [], 'interventions': [], 'invalidated_sessions': [],
            'invalidated_at': 0, 'independent_count': 0, 'last_independent_at': None, 'state': 'insufficient_evidence'})
        g['source_sessions'].append(row['id'])
        if s['needs_review']:
            g['invalidated_sessions'].append(row['id'])
            latest=c.execute("SELECT MAX(at) FROM learning_events WHERE session=? AND kind IN ('correction','memory_invalidated')",(row['id'],)).fetchone()[0]
            g['invalidated_at']=max(g['invalidated_at'],latest or 0)
            continue
        for e in c.execute("SELECT * FROM learning_events WHERE session=? AND kind='turn' ORDER BY id", (row['id'],)):
            g.setdefault('_events', []).append((e['at'],e['id'],row['id'],e['payload']))
    for g in groups.values():
        for at, event_id, session_id, payload in sorted(g.pop('_events', [])):
            p = json.loads(payload)
            ref = {'session_id': session_id, 'event_id': event_id, 'at': at}
            if p.get('status'):
                g['latest_observation']={**ref,'status':p['status'],'kind':p['evidence']}
                g['evidence'].append({**ref, 'status': p['status'], 'kind': p['evidence'],
                    'task_id':p.get('task_id'),'task_type':p.get('task',{}).get('task_type','open'),
                    'phase':p.get('task',{}).get('phase'), 'reference_origin':p.get('task',{}).get('reference_origin'),
                    'student_quote': p.get('assessment', {}).get('evidence_quote', p.get('text', ''))[:250],
                    'hypothesis': p.get('assessment', {}).get('hypothesis', '')[:250], 'certainty': 'tentative','criteria':p.get('criteria_results',[])})
                if g['interventions'] and 'next_observation' not in g['interventions'][-1]:
                    g['interventions'][-1]['next_observation']={**ref,'status':p['status'],'kind':p['evidence']}
                if p['evidence'] in ('independent_verified', 'independent_model_assessed'):
                    g['independent_count'] += 1
                    g['last_independent_at'] = at
                    g['state'] = 'independent_observed'
                    g['latest_independent'] = ref
                elif p['status'] in ('incorrect', 'incomplete'):
                    g['last_difficulty_at']=at
                    g['state'] = 'needs_support'
                elif p['status'] == 'correct':
                    g['state'] = 'assisted_complete' if p.get('assisted') else 'practice_complete'
            if p.get('delivered_action') or p.get('evidence') == 'dialogue_only':
                g['interventions'].append({**ref, 'action': p.get('delivered_action', p.get('action')),
                    'feedback': p.get('feedback', '')[:400], 'student_reply': p.get('text', '')[:250],
                    'effect': 'not_established'})
    for g in groups.values():
        g['independent_current']=bool(g['last_independent_at'] and g['last_independent_at']>=max(g.get('last_difficulty_at',0),g['invalidated_at']))
        g['review_due'] = bool(g['last_independent_at'] and now - g['last_independent_at'] >= 86400)
        if g['invalidated_sessions'] and (not g['last_independent_at'] or g['invalidated_at']>=g['last_independent_at']):
            g['state'] = 'needs_recheck'
        g['next_step'] = ('重新独立检查修订后的知识点' if g['state'] == 'needs_recheck' else
            '安排延迟复习题' if g['review_due'] else
            '补充先修诊断并换用教学方法' if g['state'] == 'needs_support' else
            '安排新的迁移检查题' if g['independent_count'] else '完成教学后安排独立检查')
        g['evidence'] = g['evidence'][-12:]
        g['interventions'] = g['interventions'][-6:]
    return list(groups.values())
