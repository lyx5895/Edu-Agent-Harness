import json
import re

import yaml
from jinja2 import Template

from .common import SOURCES, final_number


def mathtutorbench_prompt(config, **fields):
    spec = yaml.safe_load((SOURCES / 'mathtutorbench/configs' / config).read_text())
    shots = spec.get('few_shot_samples') or ''
    return Template(spec['system_prompt']).render(shots=shots, **fields).strip()


def stepverify():
    """Same expansion as MathTutorBench: each problem yields one erroneous and one correct solution."""
    items = []
    for index, row in enumerate(json.loads((SOURCES / 'stepverify/stepverify.json').read_text())):
        dialog = row.get('dialog_history', [])
        cutoff = next((i for i, turn in enumerate(dialog) if turn['user'] == 'Student'), len(dialog))
        teacher = '\n'.join(f'Teacher: {t["text"]}' for t in dialog[:cutoff])

        def steps(parts):
            return '\\n'.join(f'Step {i + 1} - {s}' for i, s in enumerate(parts))

        common = {
            'problem_id': index,
            'question': row['problem'],
            'dialog_history': teacher,
            'reference_solution': row['reference_solution'],
            'gold_answer': final_number(row['reference_solution'].split('\n')[-1]),
        }
        items.append(
            {
                **common,
                'id': f'{index}-error',
                'is_error': True,
                'error_step': int(row['incorrect_index']) + 1,
                'student_solution': steps(row['student_incorrect_solution'][:-1]),
                'student_chat_solution': dialog[cutoff]['text'] if cutoff < len(dialog) else '',
                'error_description': row.get('error_description', ''),
                'error_category': row.get('error_category', ''),
            }
        )
        items.append(
            {
                **common,
                'id': f'{index}-correct',
                'is_error': False,
                'error_step': 0,
                'student_solution': steps(row['reference_solution'].split('\n')[:-1]),
                'student_chat_solution': row['student_correct_response'],
            }
        )
    return items


def stepverify_problems():
    """Erroneous solutions with a known misconception, used to seed simulated students."""
    return [x for x in stepverify() if x['is_error'] and x['student_chat_solution']]


def mrbench():
    rows = json.loads((SOURCES / 'UnifyingAITutorEvaluation/MRBench/MRBench_V2.json').read_text())
    for row in rows:
        match = re.search(r'The question is:\s*(.*?)\s*\n\s*Student:', row['conversation_history'], re.S)
        row['question'] = match.group(1).strip() if match else ''
        row['student_turns'] = [
            t.strip()
            for t in re.split(r'\n\s*(?=Tutor:|Student:)', row['conversation_history'])
            if t.strip().startswith('Student:')
        ]
        row['gold_answer'] = final_number(
            row['Ground_Truth_Solution'].split('\n')[-1] if row.get('Ground_Truth_Solution') else ''
        )
    return rows
