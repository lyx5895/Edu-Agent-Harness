"""Next step (下一步) and stage status: where the teacher is in the loop 01-05."""

from __future__ import annotations


def _step(label, tab, target, hint='', optional=False):
    """A next-step suggestion: the UI jumps to ``tab`` and highlights the element ``target``."""
    step = {'label': label, 'tab': tab, 'target': target, 'hint': hint}
    if optional:
        step['optional'] = True  # shown as a side link; never blocks "接着做"
    return step


def next_steps(st):
    """What the teacher should do next, following the loop 备课设计 → 出题改题 → 作业批改 → 学情分析 → 讲题答疑 → 讲评后再练.

    ``st`` carries project-level counts and the counts of the latest round ``r``; later rounds restart at 出题改题.
    Pending A/B choices and stale results come first.
    """
    steps = []
    if st['pending_ab']:
        steps.append(_step('选择 A/B 候选题', 'questions', '#ab-panel', '先在两版候选题中选一版'))
    if st['stale']:
        steps.append(
            _step('处理依据已变化的产物', 'records', '#stale-list', f'{len(st["stale"])} 个产物的上游有实质修改')
        )
    c, rc, r = st['counts'], st['round'], st['r']
    label_r = f'（第 {r} 轮）' if r > 1 else ''
    if not c['lesson_plan']['confirmed']:
        if c['lesson_plan']['total']:
            steps.append(
                _step('确认备课方案', 'prep', '#lesson-plan', '核对目标、重点难点和课堂活动，整体确认后用于出题')
            )
        elif not st['has_context']:
            steps.append(_step('填写课时信息', 'prep', '#course-module', '至少填写知识模块，或添加参考材料'))
        else:
            steps.append(
                _step('生成备课方案', 'prep', '#draft-lesson-plan', '目标、重点难点和课堂活动一起生成，之后可以编辑')
            )
        return steps
    if not rc['question']['confirmed']:
        if rc['question']['total']:
            steps.append(_step('确认题目' + label_r, 'questions', '#questions', '核对题目、答案和评分要点后确认'))
        else:
            steps.append(_step('按目标出题', 'questions', '#generate-questions', '也可以导入现成试卷或手工添加题目'))
        return steps
    if not rc['submission']['total']:
        if not st['learners']:
            steps.append(_step('添加学生名单', 'grading', '#roster', '每行一个学生代号，不要写真实姓名'))
        else:
            steps.append(_step('导入学生作答' + label_r, 'grading', '#sub-csv', '每行：学生代号,题号,作答'))
    elif rc['grading']['total'] < rc['submission']['total']:
        steps.append(_step('依据标准批改' + label_r, 'grading', '#grade-round'))
    elif st['queue']:
        steps.append(_step('处理复核队列', 'grading', '#queue', f'{st["queue"]} 份批改需要老师判定'))
    elif rc['grading']['confirmed'] < rc['grading']['total']:
        steps.append(_step('确认批改', 'grading', '#confirm-auto', '复核队列以外的批改可以批量确认'))
    elif not rc['analysis']['total']:
        steps.append(_step('汇总批改结果' + label_r, 'analysis', '#analyze'))
    else:
        if r > 1 and not rc['comparison']['total']:
            steps.append(_step(f'对比第 {r - 1}、{r} 轮', 'analysis', '#compare', '看讲评和再练之后问题是否还在'))
        if rc['problem_pattern']['total'] and not rc['problem_pattern']['confirmed']:
            steps.append(_step('确认问题模式', 'analysis', '#patterns', '确认后才能据此准备讲评'))
        elif not rc['problem_pattern']['confirmed']:
            steps.append(_step('查看学情分析', 'analysis', '#analysis', '这一轮没有归纳出需要讲评的问题'))
        elif not rc['review_plan']['total']:
            steps.append(_step('定位讲解重点，准备讲评', 'review', '#draft-review'))
        elif not rc['review_plan']['confirmed']:
            steps.append(_step('确认讲评稿', 'review', '#review-plans', '编辑后确认'))
        else:
            steps.append(_step('讲评后再练', 'review', '#practice', '针对这些问题出一轮再练题，回到出题改题'))
    return steps


def stage_status(counts, rc, r):
    """Which loop stages are done for the latest round (drives the step navigation and "接着做")."""
    graded = rc['grading']['total'] and rc['grading']['confirmed'] == rc['grading']['total']
    return {
        'prep': bool(counts['lesson_plan']['confirmed']),
        'questions': bool(rc['question']['confirmed']),
        'grading': bool(rc['submission']['total'] and graded and rc['grading']['total'] >= rc['submission']['total']),
        'analysis': bool(
            rc['analysis']['total'] and (rc['problem_pattern']['confirmed'] or not rc['problem_pattern']['total'])
        ),
        'review': bool(rc['review_plan']['confirmed']),
        'round': r,
    }
