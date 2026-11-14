"""Build the Chinese and multi-subject correctness sets with Qwen3.5-27B.

stepverify-zh: the 200 StepVerify problems used by the correctness track, translated;
  labels come from the original data, and items whose numbers change in translation are dropped.
multisubject-zh: generated question / reference / correct answer / wrong answer per topic;
  kept only if the model, solving the question without the reference, agrees with the reference,
  and, given the reference, re-judges both answers as labelled.
Both are machine-made; results on them are indicative, not a human-validated benchmark.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.common import chat_json, final_number, parallel, sample
from bench.data import stepverify

OUT = Path(__file__).resolve().parent / 'data'

TRANSLATE = {
    'type': 'object',
    'properties': {
        'question': {'type': 'string', 'maxLength': 1200},
        'reference_solution': {'type': 'string', 'maxLength': 2000},
        'student_solution': {'type': 'string', 'maxLength': 2500},
    },
    'required': ['question', 'reference_solution', 'student_solution'],
    'additionalProperties': False,
}

TOPICS = {
    '物理': ['速度与路程', '密度', '压强', '欧姆定律', '功和功率', '浮力', '光的反射与折射', '比热容'],
    '化学': ['化学式与相对分子质量', '质量守恒', '酸碱中和', '溶液浓度计算', '金属活动性', '氧化还原判断'],
    '生物': ['光合作用', '呼吸作用', '遗传与性状', '食物链与能量流动', '细胞结构', '人体消化'],
    '语文': ['文言实词理解', '修辞手法辨析', '说明方法', '论证方法', '病句修改', '诗句情感分析'],
    '英语': ['一般过去时', '现在完成时', '被动语态', '定语从句', '比较级与最高级', '非谓语动词'],
}
ITEM = {
    'type': 'object',
    'properties': {
        'question': {'type': 'string', 'maxLength': 400},
        'reference': {'type': 'string', 'maxLength': 400},
        'rubric': {'type': 'string', 'maxLength': 200},
        'correct_answer': {'type': 'string', 'maxLength': 400},
        'wrong_answer': {'type': 'string', 'maxLength': 400},
        'error_description': {'type': 'string', 'maxLength': 200},
    },
    'required': ['question', 'reference', 'rubric', 'correct_answer', 'wrong_answer', 'error_description'],
    'additionalProperties': False,
}
AUDIT = {
    'type': 'object',
    'properties': {'own_solution': {'type': 'string', 'maxLength': 1200}, 'reference_correct': {'type': 'boolean'}},
    'required': ['own_solution', 'reference_correct'],
    'additionalProperties': False,
}
VERDICT = {
    'type': 'object',
    'properties': {'reason': {'type': 'string', 'maxLength': 300}, 'correct': {'type': 'boolean'}},
    'required': ['reason', 'correct'],
    'additionalProperties': False,
}


def translate(problem):
    error, right = problem
    out = {}
    for item in (error, right):
        result = chat_json(
            'judge',
            [
                {
                    'role': 'system',
                    'content': '把下面的数学题、参考解答和学生解答忠实翻译成简体中文。数字、算式和计算结果（包括学生算错的地方）必须原样保留，'
                    '不要改正学生的错误，不要增删步骤。',
                },
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'question': item['question'],
                            'reference_solution': item['reference_solution'],
                            'student_solution': item['student_chat_solution'],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            'translation',
            TRANSLATE,
            max_tokens=3000,
        )
        if final_number(result['student_solution']) != final_number(item['student_chat_solution']):
            return None
        out[item['id']] = {
            **item,
            'question': result['question'],
            'reference_solution': result['reference_solution'],
            'student_chat_solution': result['student_solution'],
            'dialog_history': '',
            'language': 'zh',
        }
    # Both versions must share one question/reference wording.
    e, r = out[error['id']], out[right['id']]
    r.update(question=e['question'], reference_solution=e['reference_solution'])
    return [e, r]


def reference_holds(item):
    own = chat_json(
        'judge',
        [
            {
                'role': 'system',
                'content': '先不看参考答案，自己独立完整地解答题目（写在own_solution，计算题逐步算出数值和单位）；'
                '然后对照参考答案，判断参考答案在事实、计算和结论上是否完全正确。',
            },
            {
                'role': 'user',
                'content': json.dumps(
                    {'question': item['question'], 'reference': item['reference_solution'], 'rubric': item['rubric']},
                    ensure_ascii=False,
                ),
            },
        ],
        'audit',
        AUDIT,
        max_tokens=2000,
    )
    return own['reference_correct']


# Removed after a manual read of the physics/chemistry items: flawed premise or disputable reference.
MANUAL_DROPS = {'物理-压强-1', '物理-光的反射与折射-1', '物理-光的反射与折射-2', '化学-酸碱中和-1'}


def audit(path):
    rows = [
        json.loads(line) for line in path.read_text().splitlines() if json.loads(line)['problem_id'] not in MANUAL_DROPS
    ]
    problems = {r['problem_id']: r for r in rows}
    verdicts = dict(zip(problems, parallel(reference_holds, list(problems.values()), 16)))
    kept = [r for r in rows if verdicts[r['problem_id']] is True]
    path.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in kept))
    dropped = sorted(p for p, v in verdicts.items() if v is not True)
    print(f'{path.name}: kept {len(kept)} items, dropped {len(dropped)} problems:', dropped)


def generate(spec):
    subject, topic, k = spec
    item = chat_json(
        'judge',
        [
            {
                'role': 'system',
                'content': '你是初中学科教师，出一道简答题用于检验AI判卷。给出参考答案、简短评分要点、一份完全正确的学生作答，'
                '以及一份含一处典型错误的学生作答（错误要具体、像真实学生，不要在作答里自我纠正）。作答用学生口吻，1-3句。',
            },
            {'role': 'user', 'content': f'学科：{subject}；知识点：{topic}；第{k + 1}题，题目不要和常见例题完全相同。'},
        ],
        'item',
        ITEM,
        max_tokens=1500,
        temperature=0.8,
    )
    verdicts = []
    for answer in (item['correct_answer'], item['wrong_answer']):
        v = chat_json(
            'judge',
            [
                {'role': 'system', 'content': '根据参考答案和评分要点判断学生作答是否完全正确。'},
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'question': item['question'],
                            'reference': item['reference'],
                            'rubric': item['rubric'],
                            'student': answer,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            'verdict',
            VERDICT,
            max_tokens=600,
        )
        verdicts.append(v['correct'])
    if verdicts != [True, False]:
        return None
    base = {
        'problem_id': f'{subject}-{topic}-{k}',
        'subject': subject,
        'question': item['question'],
        'reference_solution': item['reference'],
        'rubric': item['rubric'],
        'dialog_history': '',
        'language': 'zh',
    }
    return [
        {
            **base,
            'id': base['problem_id'] + '-correct',
            'is_error': False,
            'student_chat_solution': item['correct_answer'],
        },
        {
            **base,
            'id': base['problem_id'] + '-error',
            'is_error': True,
            'student_chat_solution': item['wrong_answer'],
            'error_description': item['error_description'],
        },
    ]


def main():
    OUT.mkdir(exist_ok=True)
    rows = stepverify()
    by_problem = {}
    for row in rows:
        by_problem.setdefault(row['problem_id'], {})[row['is_error']] = row
    problems = sample(sorted(by_problem), 200)
    pairs = [(by_problem[p][True], by_problem[p][False]) for p in problems]
    translated = [x for r in parallel(translate, pairs, 16) if isinstance(r, list) for x in r]
    (OUT / 'stepverify-zh.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in translated))
    print('stepverify-zh items', len(translated))
    specs = [(s, t, k) for s, topics in TOPICS.items() for t in topics for k in range(3)]
    generated = [x for r in parallel(generate, specs, 16) if isinstance(r, list) for x in r]
    (OUT / 'multisubject-zh.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in generated))
    print('multisubject-zh items', len(generated), 'from', len(specs), 'topics x 3')
    audit(OUT / 'multisubject-zh.jsonl')


if __name__ == '__main__':
    audit(OUT / 'multisubject-zh.jsonl') if sys.argv[1:] == ['audit'] else main()
