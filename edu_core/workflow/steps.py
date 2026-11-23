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
    Pending A/B choices and stale results of the current scope come first; stale results of another round or class
    follow as an optional step.
    """
    steps = []
    if st['pending_ab']:
        # A comparison is worth making, never a gate: the questions are already usable and can be confirmed as they are.
        steps.append(
            _step(
                '比较题目版本',
                'questions',
                '#ab-panel',
                f'{len(st["pending_ab"])} 道题另有一个版本，可以比较；不比较也可以继续',
                optional=True,
            )
        )
    here, elsewhere = split_stale(st)
    if here:
        steps.append(
            _step('更新受影响的结果', stale_tab(here), '.stale-box', f'{len(here)} 项结果用的是改动前的题目或评分标准')
        )
    steps += _forward(st)
    if elsewhere:
        # Results of an earlier round or another class: worth tidying up, but they don't hold up this round.
        steps.append(
            _step(
                '更新其他班级或轮次的结果',
                stale_tab(elsewhere),
                '.stale-box',
                f'{len(elsewhere)} 项其他班级或早先一轮的结果需要更新',
                optional=True,
            )
        )
    return steps


# The stage a stale result is shown and redone on (the 需要更新 card at the top of that page).
STALE_TAB = {
    'lesson_plan': 'prep',
    'objective': 'prep',
    'question': 'questions',
    'rubric': 'questions',
    'grading': 'grading',
    'analysis': 'analysis',
    'problem_pattern': 'analysis',
    'comparison': 'analysis',
    'review_plan': 'review',
    'explanation': 'review',
}
LOOP = ('prep', 'questions', 'grading', 'analysis', 'review')


def stale_tab(marks):
    """The earliest stage of the loop holding one of these stale results: redo upstream first."""
    tabs = {STALE_TAB.get(m.get('type'), 'prep') for m in marks}
    return next(t for t in LOOP if t in tabs)


def split_stale(st):
    """Stale marks that concern the scope being worked on (this assignment and round, or shared content such as
    questions and rubrics) versus results of another round or class."""
    from edu_core.artifacts import ASSIGNMENT_TYPES

    aid, r = st.get('aid') or '', st['r']
    here, elsewhere = [], []
    for m in st['stale']:
        own = m.get('type') not in ASSIGNMENT_TYPES or (
            (m.get('assignment_id') or '') == aid and (m.get('round') or 1) == r
        )
        (here if own else elsewhere).append(m)
    return here, elsewhere


def _forward(st):
    """The loop's next step in the current scope."""
    steps = []
    c, rc, r = st['counts'], st['round'], st['r']
    label_r = f'（第 {r} 轮）' if r > 1 else ''
    if st.get('task_kind') == 'grading' and not rc['question']['total']:
        steps.append(_step('导入现成试卷', 'grading', '#g-paper-text', '核对题目与评分标准后，即可导入作答'))
        return steps
    if st.get('task_kind') == 'explain' and not rc['problem_pattern']['confirmed']:
        steps.append(_step('准备题目讲解', 'review', '#ex-question'))
        return steps
    if st.get('task_kind') == 'practice' and not rc['question']['total'] and not st.get('plan_pending'):
        # 出一份练习 starts on 02: without a lesson plan the objectives are inferred from the request.
        if st['has_context']:
            steps.append(
                _step('按要求出题', 'questions', '#generate-questions', '没有备课方案时，会先从要求反推教学目标')
            )
        else:
            steps.append(_step('写下练习的主题', 'questions', '#q-topic', '也可以导入现成试卷'))
        return steps
    if not c['lesson_plan']['confirmed'] and not rc['question']['total']:
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
            steps.append(_step('确认共性问题', 'analysis', '#patterns', '确认后才能据此准备讲评'))
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


PAPER_ALT = {'label': '导入现成试卷', 'tab': 'questions', 'target': '#paper-text'}
# On 03 the paper is imported right in the page (缺什么就在当前页补): the questions it yields show in 02.
PAPER_HERE = {'label': '导入现成试卷', 'tab': 'grading', 'target': '#g-paper-text'}


def _blocker(reason, action, alt=None):
    out = {'blocked': True, 'reason': reason, 'action': action}
    if alt:
        out['alt'] = alt
    return out


def _open():
    return {'blocked': False}


def blockers(st):
    """Per stage: is the page usable yet, and if not, why and what to click.

    Tasks started from the home page (出一份练习、批改作业) land on a later stage; instead of a greyed-out page the
    teacher sees the one missing step and, where it exists, the way round the lesson plan (importing a paper).
    """
    c, rc = st['counts'], st['round']
    out = {'prep': _open(), 'review': _open()}  # 直接准备讲解 needs no earlier stage

    # 出题 does not wait for a lesson plan: without one, the objectives are inferred from the request (marked so).
    # Only a plan drafted in 01 and not yet confirmed has to be confirmed first.
    if st.get('plan_pending') and not c['question']['total']:
        out['questions'] = _blocker(
            '备课方案还是草稿：确认后再按目标出题。', _step('确认备课方案', 'prep', '#lesson-plan'), PAPER_ALT
        )
    else:
        out['questions'] = _open()

    if rc['question']['confirmed']:
        out['grading'] = _open()
    elif rc['question']['total']:
        out['grading'] = _blocker(
            '本轮的题目还没有确认：批改依据已确认的题目和评分标准。', _step('确认题目', 'questions', '#questions')
        )
    else:
        # A 批改 task usually arrives with a paper: importing it comes first. Any other task carries on with its own
        # plan or questions, and importing a paper is the way round.
        own = (
            out['questions']['action']
            if out['questions']['blocked']
            else _step('出题', 'questions', '#generate-questions')
        )
        reason = (
            '还没有题目：批改需要题目和评分标准。'
            if out['questions']['blocked']
            else '本轮还没有题目：批改需要题目和评分标准。'
        )
        first, second = (PAPER_HERE, own) if st.get('task_kind') == 'grading' else (own, PAPER_HERE)
        out['grading'] = _blocker(reason, first, second)

    graded = rc['grading']['total'] and rc['grading']['confirmed'] == rc['grading']['total']
    if rc['submission']['total'] and graded and rc['grading']['total'] >= rc['submission']['total']:
        out['analysis'] = _open()
    elif out['grading']['blocked']:
        # Seen from 04, the missing piece is two stages back: say where it is instead of "导入现成试卷".
        out['analysis'] = _blocker(
            '还没有可以分析的批改结果：先要有确认过的题目，再导入作答并批改。',
            _step('先到 02 确认题目', 'questions', '#questions')
            if rc['question']['total']
            else _step('先到 02 准备题目', 'questions', '#generate-questions'),
        )
    elif not rc['submission']['total']:
        act = (
            _step('导入学生作答', 'grading', '#sub-csv')
            if st['learners']
            else _step('添加学生名单', 'grading', '#roster')
        )
        out['analysis'] = _blocker('本轮还没有学生作答。', act)
    elif rc['grading']['total'] < rc['submission']['total']:
        out['analysis'] = _blocker('还有作答没有批改。', _step('批改本轮作答', 'grading', '#grade-round'))
    elif st['queue']:
        out['analysis'] = _blocker(f'{st["queue"]} 份批改需要你判定。', _step('处理复核队列', 'grading', '#queue'))
    else:
        out['analysis'] = _blocker(
            '批改还没有确认：学情只用已确认的批改。', _step('确认批改', 'grading', '#confirm-auto')
        )
    return out
