"""Run the workspaces against a scripted model and a seeded demo class (no GPU needed).

    .envs/harness/bin/python scripts/ui_preview.py --port 7862 --db /tmp/omniedu-preview.sqlite

The scripted model is deterministic and only understands the seeded tasks well;
it exists for UI work, screenshots and browser checks, not for judging quality.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from harness.app import create_app
from harness.core.gateway import Gateway

CLASS = '七年一班'
SPEED = {
    'class_id': CLASS,
    'course_id': '七年级数学',
    'concept_id': '平均速度',
    'domain': '数学',
    'task_type': 'problem_solving',
    'objective': '能根据路程和时间求平均速度，并写出计算过程和单位',
    'question': '小明骑车 3 小时行了 45 千米，他的平均速度是多少千米每小时？请写出计算过程和单位。',
    'rubric': '列出“速度 = 路程 ÷ 时间”，代入数据计算，结果带单位。',
    'reference': '45 ÷ 3 = 15，平均速度是 15 千米/小时。',
    'check_requirements': ['能在新的情境中用路程除以时间求速度，并写出单位'],
}
UNITS = {
    'class_id': CLASS,
    'course_id': '七年级科学',
    'concept_id': '月相',
    'domain': '科学',
    'task_type': 'concept',
    'objective': '能用日、地、月的相对位置解释月相变化',
    'question': '为什么我们看到的月亮形状会在一个月里变化？请用太阳、地球和月球的位置来解释。',
    'rubric': '说明月球本身不发光、被太阳照亮的一半朝向地球的部分随位置变化。',
    'reference': '月球反射太阳光，绕地球运行时被照亮的半球朝向地球的部分不断变化，所以看到的形状变化。',
}


def numbers(text):
    return set(re.findall(r'\d+(?:\.\d+)?', text))


class ScriptedModel:
    """Deterministic stand-in for OmniEdu-9B with plausible Chinese tutoring replies."""

    base_url = 'scripted-preview'
    model_name = 'scripted-preview'

    def __init__(self):
        self.checks = 0

    def plan(self, request, sources, mode='problem', previous=None):
        if previous:
            self.checks += 1
            distance, hours = [(24, 2), (60, 4), (36, 3)][self.checks % 3]
            return {
                'domain': '数学',
                'objective': previous['objective'],
                'question': f'一辆电动车 {hours} 小时行驶了 {distance} 千米，它的平均速度是多少千米每小时？写出过程和单位。',
                'rubric': '路程除以时间，结果带单位。',
                'reference': f'{distance} ÷ {hours} = {distance // hours}，平均速度是 {distance // hours} 千米/小时。',
                'source_quote': '',
                'prerequisite': '除法',
                'algebra_initial': '',
                'algebra_kind': 'none',
                'check_requirements': previous.get('contract', {}).get('check_requirements', []),
            }
        question = request.split('\n')[0]
        return {
            'domain': '数学' if re.search(r'\d', question) else '综合',
            'objective': '弄清题目考查的关系，并写出有依据的解答',
            'question': question if mode == 'problem' else f'根据材料，{question}',
            'rubric': '步骤完整，结论有依据。',
            'reference': '（模型参考答案，待教师审核）',
            'source_quote': '',
            'prerequisite': '',
            'algebra_initial': '',
            'algebra_kind': 'none',
        }

    def assess(self, task, text, context, verification):
        expected = numbers(task['reference'])
        if expected:
            correct = expected <= numbers(text) and bool(re.search(r'[÷/除]', text))
        else:
            correct = len(text) > 30 and any(k in text for k in ('太阳', '反射', '照亮'))
        quote = text[:60]
        return {
            'status': 'correct' if correct else 'incorrect',
            'confidence': 'high',
            'evidence_quote': quote,
            'observation': '列式和单位都正确。' if correct else '把路程和时间相乘了，平均速度应当是路程除以时间。',
            'hypothesis': '混淆了速度与路程的关系' if not correct else '',
        }

    def assess_criterion(self, task, criterion, text, context):
        return {'status': 'correct', 'confidence': 'high', 'evidence_quote': text[:40], 'observation': '满足要求'}

    def respond(self, task, action, text, context, assessment, revision=None):
        replies = {
            'locate': '你算出了 45 乘 3 的结果，但平均速度表示每小时走多少千米。把 45 千米平均分到 3 个小时里，每小时走的路程会比 45 千米多还是少？',
            'probe': '你能说说为什么这里要用除法吗？',
            'clarify': '能把你的计算过程写得再完整一点吗？比如先写出用到的关系式。',
            'scaffold_concept': '先回想“速度”的意思：它表示单位时间里走过的路程。所以要知道每小时走多少，就要把总路程按小时平均分。你觉得该用哪种运算？',
            'scaffold_step': '第一步写出关系式：速度 = 路程 ÷ 时间。接下来把题目里的路程和时间分别代进去，算一算。',
            'scaffold_example': '看一个类似的例子：小华 2 小时走了 8 千米，速度是 8 ÷ 2 = 4 千米/小时。照这个样子，试着算一下小明的速度。',
            'explain': '平均速度 = 总路程 ÷ 总时间。小明 3 小时行了 45 千米，所以速度是 45 ÷ 3 = 15，单位是千米/小时。注意不要把路程和时间相乘，那样得到的不是速度。',
            'change_explanation': '换个角度想：如果每小时走 15 千米，3 小时一共走多少？用这个关系反过来想一想，速度怎么求？',
            'prerequisite': '我们先确认一个基础：把 45 平均分成 3 份，每份是多少？',
        }
        return replies.get(action, '先写出你用到的关系式，再一步一步算。你觉得第一步是什么？')

    def review(self, task, candidate, action, student=None):
        return {'acceptable': True, 'reason': '未泄露答案，语气合适'}

    def interpret_reply(self, packet):
        return {'outcome': 'misconception', 'evidence_quote': packet['student_reply'], 'hypothesis': '仍在混淆乘除'}

    def translate(self, text, target='English'):
        return text

    def transcribe(self, url):
        return {'text': '45 ÷ 3 = 15\n平均速度是 15 千米/小时', 'uncertain': False, 'note': ''}

    def guess_knowledge(self, question, subject):
        return []

    def choose_knowledge(self, question, subject, candidates):
        return []


def operate(open_engine, sid, kind, text=''):
    s = open_engine.session(sid)
    return open_engine.operation(sid, f'seed-{sid}-{s["version"]}-{kind}', kind, {'text': text}, s['version'])


def seed(gateway):
    """A small class with varied progress, so every teacher view has something to show."""
    e = gateway.open
    if e.assignments(CLASS):
        return
    speed = e.publish_assignment(SPEED)['assignment_id']
    moon = e.publish_assignment(UNITS)['assignment_id']
    due = time.time() + 2 * 86400
    learners = ['林小雨', '陈一鸣', '王可', '赵子涵', '周宁']
    e.assign_followups(
        dict(
            class_id=CLASS,
            assignment_id=speed,
            learners=learners,
            kind='remediation',
            due_at=due,
            reason='单元前测：速度公式',
        ),
        'seed-speed',
    )
    e.assign_followups(
        dict(
            class_id=CLASS,
            assignment_id=moon,
            learners=learners[:3],
            kind='remediation',
            due_at=due + 86400,
            reason='月相概念',
        ),
        'seed-moon',
    )

    def start(learner):
        followup = next(f for f in e.inbox(CLASS, learner) if f['course_id'] == '七年级数学')
        return e.create(learner, followup_id=followup['followup_id'])['session_id']

    # 林小雨: correct first time, then passes an independent check.
    sid = start('林小雨')
    operate(e, sid, 'attempt', '速度 = 路程 ÷ 时间，45 ÷ 3 = 15，平均速度是 15 千米/小时。')
    operate(e, sid, 'check')
    check = e.session(sid)['task']['question']
    distance, hours = re.findall(r'\d+', check)[1], re.findall(r'\d+', check)[0]
    operate(
        e,
        sid,
        'attempt',
        f'{distance} ÷ {hours} = {int(distance) // int(hours)}，平均速度是 {int(distance) // int(hours)} 千米/小时。',
    )
    # 陈一鸣: multiplies instead of dividing, twice.
    sid = start('陈一鸣')
    operate(e, sid, 'attempt', '平均速度等于路程乘以时间，45 × 3 = 135，所以平均速度是 135 千米每小时。')
    operate(e, sid, 'reply', '因为要把路程和时间合起来算')
    operate(e, sid, 'attempt', '45 × 3 = 135 千米/小时')
    # 王可: needs hints, then succeeds with help.
    sid = start('王可')
    operate(e, sid, 'attempt', '45 + 3 = 48')
    operate(e, sid, 'hint')
    operate(e, sid, 'hint')
    operate(e, sid, 'attempt', '速度 = 路程 ÷ 时间 = 45 ÷ 3 = 15 千米/小时')
    # 赵子涵: one wrong attempt, still working.
    sid = start('赵子涵')
    operate(e, sid, 'attempt', '时间除以路程：3 ÷ 45 ≈ 0.07')
    e.save_pack(
        dict(
            class_id=CLASS,
            title='第三章 速度与时间',
            items=[
                {**SPEED, 'reference_origin': 'model_generated_unverified'},
                {
                    **SPEED,
                    'objective': '能比较两个物体的平均速度',
                    'question': '甲 2 小时走 10 千米，乙 3 小时走 12 千米，谁走得快？说明理由。',
                    'reference': '甲 10 ÷ 2 = 5 千米/小时，乙 12 ÷ 3 = 4 千米/小时，甲快。',
                    'reference_origin': 'model_generated_unverified',
                },
            ],
        ),
        'seed-pack',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=7862)
    parser.add_argument('--db', default='/tmp/omniedu-preview/learning.sqlite')
    parser.add_argument('--no-seed', action='store_true')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    gateway = Gateway(db=args.db, model=ScriptedModel())
    if not args.no_seed:
        seed(gateway)
    uvicorn.run(create_app(lambda: gateway), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
