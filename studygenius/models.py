"""Validated contracts. Models supply content/data, never executable code or document templates."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class JobOptions(Contract):
    title: str = Field(default="La mia dispensa", min_length=1, max_length=160)
    exam_brief: str = Field(default="", max_length=12000)
    mode: Literal["live", "demo"] = "live"
    review_rounds: int = Field(default=2, ge=1, le=4)
    max_api_calls: int = Field(default=300, ge=1, le=10000)
    max_total_tokens: int = Field(default=2_000_000, ge=1000, le=100_000_000)
    pages_per_batch: int = Field(default=2, ge=1, le=4)


class SourcePage(Contract):
    id: str
    document: str
    filename: str
    number: int
    text: str
    image: str


class Topic(Contract):
    title: str = Field(min_length=1, max_length=250)
    content: str = Field(min_length=10, max_length=35000)
    kind: Literal["theory", "derivation", "exercise", "example", "definition"]


class SourceVisual(Contract):
    title: str = Field(min_length=1, max_length=250)
    # x0, y0, x1, y1 in thousandths of the displayed page.
    bbox: list[int] = Field(min_length=4, max_length=4)
    description: str = Field(min_length=10, max_length=12000)
    uncertainty: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def valid_box(self):
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            raise ValueError("bbox deve essere [x0,y0,x1,y1] tra 0 e 1000 con area positiva")
        return self


class PageAnalysis(Contract):
    page_id: str
    topics: list[Topic] = Field(max_length=40)
    visuals: list[SourceVisual] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list)
    excluded_reason: str = ""

    @model_validator(mode="after")
    def accounted_for(self):
        if not self.topics and not self.excluded_reason.strip():
            raise ValueError("Una pagina senza argomenti deve avere un motivo di esclusione")
        if self.topics and self.excluded_reason:
            raise ValueError("Una pagina con argomenti non può essere esclusa")
        if self.visuals and not self.topics:
            raise ValueError("Le figure didattiche devono appartenere a una pagina con argomenti")
        return self


class EvidenceBatch(Contract):
    pages: list[PageAnalysis] = Field(min_length=1, max_length=4)


class ChapterPlan(Contract):
    title: str = Field(min_length=1, max_length=180)
    topic_ids: list[str] = Field(min_length=1, max_length=10)
    objectives: list[str] = Field(min_length=1, max_length=8)
    prerequisites: list[str] = Field(default_factory=list, max_length=8)


class Outline(Contract):
    chapters: list[ChapterPlan] = Field(min_length=1, max_length=150)


class CourseGuide(Contract):
    conventions: list[str] = Field(max_length=80)
    symbols: list[str] = Field(max_length=120)
    conflicts: list[str] = Field(max_length=60)


class Equation(Contract):
    latex: str = Field(min_length=1, max_length=4000)
    explanation: str = Field(min_length=5)
    symbols: list[str] = Field(min_length=1)
    assumptions: str = Field(min_length=1)


class Step(Contract):
    label: str = Field(min_length=1, max_length=180)
    explanation: str = Field(min_length=5)
    math: str = Field(default="", max_length=4000)


class Section(Contract):
    title: str = Field(min_length=1, max_length=180)
    topic_ids: list[str] = Field(min_length=1)
    paragraphs: list[str] = Field(min_length=1)
    equations: list[Equation] = Field(default_factory=list)
    derivation: list[Step] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)


class Exercise(Contract):
    question: str = Field(min_length=10)
    origin: Literal["source", "generated"]
    topic_ids: list[str] = Field(min_length=1)
    requested_points: list[str] = Field(min_length=1)
    steps: list[Step] = Field(min_length=2)
    answers: list[str] = Field(min_length=1)
    checks: list[str] = Field(min_length=1)


class Recall(Contract):
    question: str = Field(min_length=5)
    answer: str = Field(min_length=5)


class VisualExplanation(Contract):
    visual_id: str
    how_to_read: list[str] = Field(min_length=2)
    meaning: str = Field(min_length=10)
    takeaways: list[str] = Field(min_length=1)
    limitations: str = Field(min_length=1)


class Series(Contract):
    label: str = Field(min_length=1, max_length=150)
    x: list[float] = Field(min_length=2, max_length=2000)
    y: list[float] = Field(min_length=2, max_length=2000)

    @model_validator(mode="after")
    def same_size(self):
        if len(self.x) != len(self.y):
            raise ValueError("Le serie x e y devono avere la stessa lunghezza")
        return self


class Chart(Contract):
    title: str = Field(min_length=1, max_length=180)
    topic_ids: list[str] = Field(min_length=1)
    xlabel: str = Field(min_length=1, max_length=180)
    ylabel: str = Field(min_length=1, max_length=180)
    kind: Literal["line", "scatter"] = "line"
    series: list[Series] = Field(min_length=1, max_length=6)
    provenance: str = Field(min_length=10)
    explanation: str = Field(min_length=10)


class Lesson(Contract):
    title: str = Field(min_length=1, max_length=180)
    introduction: str = Field(min_length=20)
    sections: list[Section] = Field(min_length=1, max_length=30)
    exercises: list[Exercise] = Field(min_length=1, max_length=20)
    recall: list[Recall] = Field(min_length=3, max_length=20)
    visuals: list[VisualExplanation] = Field(default_factory=list, max_length=60)
    charts: list[Chart] = Field(default_factory=list, max_length=6)
    recap: list[str] = Field(min_length=2)
    uncertainties: list[str] = Field(default_factory=list)


class Issue(Contract):
    severity: Literal["blocker", "major", "minor"]
    target: str
    message: str
    correction: str


class Review(Contract):
    passed: bool
    coverage: int = Field(ge=0, le=100)
    correctness: int = Field(ge=0, le=100)
    clarity: int = Field(ge=0, le=100)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return (self.passed and min(self.coverage, self.correctness, self.clarity) >= 90
                and not any(i.severity in ("blocker", "major") for i in self.issues))
