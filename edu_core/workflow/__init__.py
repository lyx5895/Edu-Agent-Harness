"""Teacher workflow (design doc 4): 01 备课设计 -> 02 出题改题 -> 03 作业批改 -> 04 学情分析 -> 05 讲题答疑.

Every step reads confirmed upstream artifacts through a whitelisted :class:`Context`, calls the model
outside any transaction, then writes artifacts, references and generation records in one transaction.

:class:`Workspace` is composed of one mixin per stage:

* ``schemas`` / ``prompts``: model output schemas and system prompts
* ``common``: helpers shared by stages; ``steps``: next step (下一步) and stage status
* ``base``: store and model plumbing, artifact operations, lesson plan lookups
* ``projects``: projects, materials, curriculum picks, 进入下一课时
* ``lesson_plan`` (01), ``questions`` (02), ``submissions`` (03), ``analysis`` (04), ``review`` (05)
* ``photos``: photo import (transcribe, then the teacher checks the text before import)
* ``records``: staleness, overview and research export
"""

from edu_core.workflow.analysis import AnalysisMixin
from edu_core.workflow.base import WorkspaceBase
from edu_core.workflow.common import (
    Conflict,
    normalize_plan,
    usable_drafts,
)
from edu_core.workflow.lesson_plan import LessonPlanMixin
from edu_core.workflow.photos import PhotosMixin
from edu_core.workflow.projects import ProjectsMixin
from edu_core.workflow.questions import QuestionsMixin
from edu_core.workflow.records import RecordsMixin
from edu_core.workflow.review import ReviewMixin
from edu_core.workflow.schemas import (
    Activity,
    AnalysisOutput,
    Evidence,
    FollowUp,
    ImportedPaper,
    LessonPlanOutput,
    ObjectiveDraft,
    PatternDraft,
    Pitfall,
    PitfallOutput,
    PlanObjective,
    PointDraft,
    QualityOutput,
    QuestionDraft,
    QuestionSet,
    QuestionType,
    ReviewOutput,
    ReviewSection,
    SolveOutput,
)
from edu_core.workflow.steps import next_steps, stage_status
from edu_core.workflow.submissions import SubmissionsMixin


class Workspace(
    ProjectsMixin,
    LessonPlanMixin,
    QuestionsMixin,
    SubmissionsMixin,
    PhotosMixin,
    AnalysisMixin,
    ReviewMixin,
    RecordsMixin,
    WorkspaceBase,
):
    """One teacher workspace over a store and a model; each stage keeps its methods in its own module."""


__all__ = [
    'Activity',
    'AnalysisOutput',
    'Conflict',
    'Evidence',
    'FollowUp',
    'ImportedPaper',
    'LessonPlanOutput',
    'ObjectiveDraft',
    'PatternDraft',
    'Pitfall',
    'PitfallOutput',
    'PlanObjective',
    'PointDraft',
    'QualityOutput',
    'QuestionDraft',
    'QuestionSet',
    'QuestionType',
    'ReviewOutput',
    'ReviewSection',
    'SolveOutput',
    'Workspace',
    'next_steps',
    'normalize_plan',
    'stage_status',
    'usable_drafts',
]
