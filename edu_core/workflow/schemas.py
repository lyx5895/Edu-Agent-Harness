"""Structured outputs the workflow asks the model for (validated with pydantic in llm.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from edu_core.subjects import QUESTION_TYPES

QuestionType = Literal[tuple(QUESTION_TYPES)]  # type: ignore[valid-type]


class ObjectiveDraft(BaseModel):
    text: str = Field(max_length=200)
    success_criterion: str = Field(max_length=300)


class PointDraft(BaseModel):
    description: str = Field(max_length=300)
    score: float = Field(gt=0, le=60)
    check_kind: Literal['none', 'numeric', 'algebra', 'chem_equation']
    check_expected: str = Field(max_length=120)


class QuestionDraft(BaseModel):
    type: QuestionType
    stem: str = Field(max_length=3000)
    options: list[str]
    answer_key: str = Field(max_length=200)
    accepted: list[str]
    reference: str = Field(max_length=3000)
    objective_index: int
    target_pattern_index: int
    points: list[PointDraft]


class QuestionSet(BaseModel):
    questions: list[QuestionDraft]


class StandardPick(BaseModel):
    ids: list[str]
    note: str = Field(max_length=200)


class InferredGoals(BaseModel):
    objectives: list[ObjectiveDraft]


class ImportedPaper(BaseModel):
    objectives: list[ObjectiveDraft]
    questions: list[QuestionDraft]


class QualityOutput(BaseModel):
    tests_objective: bool
    objective_note: str = Field(max_length=200)
    in_scope: bool
    scope_note: str = Field(max_length=200)
    difficulty: Literal['基础', '中等', '较难']
    issues: list[str]  # must be fixed before use: these make a question 需核对
    suggestions: list[str]  # could be better; shown folded, never a flag


class SolveOutput(BaseModel):
    answer: str = Field(max_length=4000)
    final_answer: str = Field(max_length=300)


class Evidence(BaseModel):
    eid: str
    quote: str = Field(max_length=300)


class PatternDraft(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(max_length=400)
    tags: list[str]
    evidence: list[Evidence]
    existing_pattern: str


class FollowUp(BaseModel):
    question: str = Field(max_length=400)
    why: str = Field(max_length=300)


class AnalysisOutput(BaseModel):
    summary: str = Field(max_length=800)
    patterns: list[PatternDraft]
    insufficient: list[FollowUp]


class Pitfall(BaseModel):
    name: str = Field(max_length=80)
    description: str = Field(max_length=400)
    question_nos: list[int]


class Activity(BaseModel):
    name: str = Field(max_length=60)
    minutes: int = Field(ge=0, le=90)
    description: str = Field(max_length=400)
    objective_index: int


class PlanObjective(ObjectiveDraft):
    standard_ids: list[str]
    beyond_standard: bool
    beyond_reason: str = Field(max_length=200)


class LessonPlanOutput(BaseModel):
    objectives: list[PlanObjective]
    key_points: list[str]
    difficulties: list[str]
    activities: list[Activity]
    notes: str = Field(max_length=600)


class ReviewSection(BaseModel):
    pattern_index: int
    title: str = Field(max_length=80)
    key_steps: list[str]
    typical_quote: str = Field(max_length=300)
    error_analysis: str = Field(max_length=800)
    follow_up: list[str]


class ReviewOutput(BaseModel):
    focus: list[str]
    sections: list[ReviewSection]
    closing: str = Field(max_length=600)


class ExplainOutput(BaseModel):
    focus: list[str]
    steps: list[str]
    pitfalls: list[str]
    answer_comment: str = Field(max_length=1200)
    quotes: list[str]
    follow_up: list[str]


class RevisedPart(BaseModel):
    text: str = Field(max_length=600)
    success_criterion: str = Field(max_length=300)
    name: str = Field(max_length=60)
    minutes: int = Field(ge=0, le=90)
    description: str = Field(max_length=400)
    items: list[str]


class PitfallOutput(BaseModel):
    pitfalls: list[Pitfall]
