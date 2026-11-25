"""Subject packs (design doc 6.1): question types, tools, error taxonomy and writing dimensions per subject.

The grading pipeline is the same for every subject; a pack only chooses which tools and tags apply.
"""

QUESTION_TYPES = {
    'single_choice': '单选题',
    'multi_choice': '多选题',
    'true_false': '判断题',
    'fill_blank': '填空题',
    'calculation': '计算与推导',
    'short_answer': '简答题',
    'material_analysis': '材料分析题',
    'language': '英语语言题（改错、语法填空、翻译）',
    'experiment': '实验设计与读图',
    'writing': '写作（作文）',
}
# Question types decided by comparing with an answer key; no model call when the key matches.
KEYED_TYPES = {'single_choice', 'multi_choice', 'true_false'}
# Writing only gets a suggested score; a teacher must confirm it.
SUGGESTED_ONLY_TYPES = {'writing'}

COMMON_TAGS = ['审题错误', '概念理解错误', '表述不完整', '结论缺失', '未作答', '其他']

_PACKS = [
    {
        'id': 'chinese',
        'name': '语文',
        'types': ['single_choice', 'fill_blank', 'short_answer', 'material_analysis', 'writing'],
        'tools': ['answer_match'],
        'tags': ['文本理解偏差', '未结合原文', '手法术语误用', '要点遗漏', '默写错误', '语言表达问题'],
        'writing_dimensions': ['内容与立意', '结构与思路', '语言表达', '书写与规范'],
    },
    {
        'id': 'math',
        'name': '数学',
        'types': ['single_choice', 'multi_choice', 'fill_blank', 'calculation', 'short_answer'],
        'tools': ['answer_match', 'numeric', 'algebra'],
        'tags': ['条件遗漏（定义域、取值范围等）', '计算错误', '公式或定理误用', '推理不严密', '分类讨论不全'],
    },
    {
        'id': 'english',
        'name': '英语',
        'types': ['single_choice', 'fill_blank', 'language', 'short_answer', 'writing'],
        'tools': ['answer_match'],
        'tags': ['词汇错误', '语法错误', '时态语态错误', '拼写错误', '语篇理解偏差', '要点遗漏'],
        'writing_dimensions': ['内容要点', '语言准确性', '词汇与句式', '篇章结构'],
        'feedback_language': 'zh',
    },
    {
        'id': 'physics',
        'name': '物理',
        'types': ['single_choice', 'multi_choice', 'fill_blank', 'calculation', 'short_answer', 'experiment'],
        'tools': ['answer_match', 'numeric', 'units', 'algebra'],
        'tags': ['物理模型建立错误', '受力分析错误', '公式误用', '单位错误', '计算错误', '实验原理不清'],
    },
    {
        'id': 'chemistry',
        'name': '化学',
        'types': ['single_choice', 'fill_blank', 'calculation', 'short_answer', 'experiment'],
        'tools': ['answer_match', 'numeric', 'units', 'chem_equation'],
        'tags': ['化学方程式错误', '配平错误', '化学式书写错误', '计算错误', '反应原理不清', '实验操作错误'],
    },
    {
        'id': 'biology',
        'name': '生物',
        'types': ['single_choice', 'fill_blank', 'short_answer', 'experiment', 'material_analysis'],
        'tools': ['answer_match'],
        'tags': ['概念混淆', '过程理解错误', '实验设计缺陷', '图表信息提取错误', '术语不规范'],
    },
    {
        'id': 'politics',
        'name': '思想政治',
        'types': ['single_choice', 'short_answer', 'material_analysis'],
        'tools': ['answer_match'],
        'tags': ['原理选择错误', '未联系材料', '观点表述不规范', '要点遗漏', '逻辑不清'],
    },
    {
        'id': 'history',
        'name': '历史',
        'types': ['single_choice', 'fill_blank', 'short_answer', 'material_analysis'],
        'tools': ['answer_match'],
        'tags': ['史实错误', '时空定位错误', '未结合史料', '因果分析不当', '要点遗漏'],
    },
    {
        'id': 'geography',
        'name': '地理',
        'types': ['single_choice', 'fill_blank', 'short_answer', 'material_analysis', 'experiment'],
        'tools': ['answer_match', 'numeric'],
        'tags': ['区域定位错误', '地理原理误用', '图表判读错误', '要素分析不全', '计算错误'],
    },
    {
        # 小学科学 (and 初中综合科学 where a region teaches it); standards: 义务教育科学课程标准 only
        'id': 'science',
        'name': '科学',
        'types': ['single_choice', 'fill_blank', 'short_answer', 'experiment', 'material_analysis'],
        'tools': ['answer_match', 'numeric', 'units'],
        'tags': ['科学概念混淆', '观察记录不准确', '实验探究不规范', '证据与结论不对应', '术语不规范'],
    },
    {
        # Knowledge questions only (e.g. SVM, decision trees, neural networks); no programming, no sandbox.
        'id': 'ai',
        'name': '人工智能',
        'types': ['single_choice', 'multi_choice', 'true_false', 'fill_blank', 'short_answer', 'calculation'],
        'tools': ['answer_match', 'numeric'],
        'tags': ['概念混淆', '原理理解错误', '算法步骤错误', '指标计算错误', '适用条件判断错误'],
    },
]

SUBJECTS = {p['id']: {'feedback_language': 'zh', 'writing_dimensions': [], **p} for p in _PACKS}
BY_NAME = {p['name']: p for p in SUBJECTS.values()}
# What the 义务教育 stage calls the same subjects (their standards are filed under these ids)
for _alias, _id in {
    '道德与法治': 'politics',
    '政治': 'politics',
    '信息科技': 'ai',
    '信息技术': 'ai',
    '生物学': 'biology',
}.items():
    BY_NAME.setdefault(_alias, SUBJECTS[_id])

# The same subject is called differently in 义务教育 (grades 1–9): what teachers, pages and prompts should say there.
COMPULSORY_NAMES = {'politics': '道德与法治', 'ai': '信息科技'}


def subject_name(subject: str, *grade_texts: str) -> str:
    """The subject's name for this grade: ``politics`` in 初二 → 道德与法治, in 高一 (or no grade) → 思想政治."""
    from edu_core.curriculum import source  # curriculum does not import subjects; kept local to stay one-way

    p = pack(subject)
    compulsory = '@' in source(p['id'], *grade_texts)
    return COMPULSORY_NAMES.get(p['id'], p['name']) if compulsory else p['name']


def subject_label(p: dict) -> str:
    """How a subject is listed in a picker: both names when the stages differ (思想政治 / 道德与法治)."""
    other = COMPULSORY_NAMES.get(p['id'])
    return f'{p["name"]} / {other}' if other else p['name']


def subject_aliases(p: dict) -> list[str]:
    """Every name that picks this subject when typed (course titles such as 初二道德与法治)."""
    return [p['name'], *(a for a, q in BY_NAME.items() if q['id'] == p['id'] and a != p['name'])]


# A subject without a pack uses the general flow (COURSE_WORKSPACE_DESIGN.md 1.2): common question types, answer-key
# matching only, the common error tags, and no curriculum standard.
GENERAL_TYPES = [
    'single_choice',
    'multi_choice',
    'true_false',
    'fill_blank',
    'short_answer',
    'calculation',
    'material_analysis',
]


def pack(subject: str) -> dict:
    """Look up a subject pack by id or Chinese name; any other name gets the general pack."""
    found = SUBJECTS.get(subject) or BY_NAME.get(subject)
    if found is not None:
        return found
    name = (subject or '').strip()
    if not name or len(name) > 20 or any(ch in name for ch in '/\\.:<>"|?*') or not name.isprintable():
        raise ValueError('请选择学科，或填写 20 字以内的学科名称')
    return {
        'id': name,
        'name': name,
        'types': GENERAL_TYPES,
        'tools': ['answer_match'],
        'tags': [],
        'feedback_language': 'zh',
        'writing_dimensions': [],
        'custom': True,
    }


def error_tags(subject: str) -> list[str]:
    p = pack(subject)
    return [*p['tags'], *COMMON_TAGS]
