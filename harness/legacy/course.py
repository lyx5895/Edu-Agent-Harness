"""Authored algebra templates. Answers/rubrics are never included in public items."""

import json
from fractions import Fraction

from harness.paths import ROOT

NAMES = [
    '正数分配律',
    '负数分配律',
    '合并同类项',
    '分数系数合并',
    '等式两边同加减',
    '等式两边同乘除',
    '两步一元方程',
    '含括号方程',
    '两边含未知数',
    '含分母方程',
]
RULES = [
    '括号外的数要乘括号内每一项。',
    '负号也要分配到每一项，注意符号变化。',
    '只有字母部分相同的项才能合并，系数相加。',
    '先通分再合并系数，常数项单独保留。',
    '等式两边同时加或减同一个数，等式仍成立。',
    '等式两边同时除以相同的非零数，等式仍成立。',
    '先消去常数项，再把未知数系数化为1。',
    '先正确去括号，再使用等式性质求解。',
    '把未知数项移到同一边，常数项移到另一边。',
    '先对等式两边同乘分母，注意括号，再求解。',
]


def build():
    skills, items = [], []
    for k in range(10):
        sid = f'k{k + 1:02}'
        skills.append(
            {
                'id': sid,
                'name': NAMES[k],
                'rule': RULES[k],
                'source': f'course:algebra-v1/{sid}',
                'prerequisites': [
                    f'k{n:02}' for n in [[], [1], [], [3], [], [5], [5, 6], [1, 2, 7], [3, 7], [1, 6, 7]][k]
                ],
            }
        )
        for j in range(5):
            # Different coefficients/constants for the two held-out checks.
            a, b, c = j + 2, j + 3, j + 5
            if k == 0:
                initial, answer, detail = f'{a}(x+{b})', f'{a}x+{a * b}', f'{a}×x + {a}×{b}'
            elif k == 1:
                initial, answer, detail = f'-{a}(x-{b})', f'-{a}x+{a * b}', f'(-{a})×x + (-{a})×(-{b})'
            elif k == 2:
                initial, answer, detail = f'{a}x+{b}x-{c}', f'{a + b}x-{c}', f'({a}+{b})x-{c}'
            elif k == 3:
                coefficient = Fraction(1, a) + Fraction(1, b)
                initial, answer, detail = f'x/{a}+x/{b}+{c}', f'{coefficient}*x+{c}', f'先计算 1/{a}+1/{b}，再乘x'
            elif k == 4:
                initial, answer, detail = f'x+{a}={a + b}', f'x={b}', f'两边同时减去{a}'
            elif k == 5:
                initial, answer, detail = f'{a}x={a * b}', f'x={b}', f'两边同时除以{a}'
            elif k == 6:
                initial, answer, detail = f'{a}x+{b}={a * c + b}', f'x={c}', f'两边先减去{b}，再除以{a}'
            elif k == 7:
                initial, answer, detail = f'{a}(x+{b})={a * (b + c)}', f'x={c}', f'去括号得到{a}x+{a * b}={a * (b + c)}'
            elif k == 8:
                initial, answer, detail = f'{a + 1}x+{b}=x+{a * c + b}', f'x={c}', f'两边先减去x，再减去{b}'
            else:
                initial, answer, detail = f'(x+{b})/{a}={c}', f'x={a * c - b}', f'两边同乘{a}，得到x+{b}={a * c}'
            phase = 'teach' if j < 3 else 'check'
            idx = j + 1 if phase == 'teach' else j - 2
            items.append(
                {
                    'id': f'{sid}-{phase}-{idx}',
                    'skill': sid,
                    'phase': phase,
                    'prompt': ('展开并合并：' if k < 2 else '化简：' if k < 4 else '解方程：') + initial,
                    'initial': initial,
                    'answer': answer,
                    'kind': 'expression' if k < 4 else 'equation',
                    'source': f'course:algebra-v1/{sid}',
                    'rubric': '每一步等价；完成指定的展开、合并或求解；结论正确。',
                    'misconception': [
                        '漏乘括号内一项',
                        '负号分配错误',
                        '不同类项合并或系数错误',
                        '分数未通分',
                        '只改变等式一侧',
                        '乘除不一致',
                        '移项符号错误',
                        '去括号错误',
                        '未知数项移项错误',
                        '去分母未整体相乘',
                    ][k],
                    'hints': [RULES[k], detail, f'{detail}。整理后得到 {answer}。将结果代回原式检查。'],
                }
            )
    return {
        'version': 'algebra-v1',
        'license': 'Project-authored educational examples',
        'skills': skills,
        'items': items,
    }


COURSE = build()
ITEMS = {i['id']: i for i in COURSE['items']}
SKILLS = {i['id']: i for i in COURSE['skills']}


def public_item(item):
    return {key: item[key] for key in ('id', 'skill', 'phase', 'prompt', 'source')}


def retrieve(skill):
    s = SKILLS[skill]
    example = ITEMS[f'{skill}-teach-1']
    return {
        'source': s['source'],
        'name': s['name'],
        'text': s['rule'],
        'example': {'problem': example['prompt'], 'worked_solution': example['hints'][2], 'source': example['id']},
    }


if __name__ == '__main__':
    (ROOT / 'course.json').write_text(json.dumps(COURSE, ensure_ascii=False, indent=2))
