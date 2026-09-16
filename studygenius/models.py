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
    pages_per_batch: int = Field(default=4, ge=1, le=4)


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


class AsseGrafico(Contract):
    label: str = Field(min_length=1, max_length=25)
    unit: str = Field(default="", max_length=20)
    scale: Literal["linear", "log"] = "linear"
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def ordered_limits(self):
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("Il limite minimo dell'asse deve precedere il massimo")
        return self


class CurvaAnalitica(Contract):
    label: str = Field(min_length=1, max_length=80)
    formula_latex: str = Field(default="", max_length=500)
    parameters: dict[str, float] = Field(default_factory=dict, max_length=24)
    x: list[float] = Field(min_length=2, max_length=1000)
    y: list[float] = Field(min_length=2, max_length=1000)
    style: Literal["solid", "dashed", "dotted"] = "solid"
    interpretation: str = Field(min_length=10, max_length=3000)

    @model_validator(mode="after")
    def aligned_samples(self):
        if len(self.x) != len(self.y):
            raise ValueError("Ogni curva deve avere lo stesso numero di coordinate x e y")
        if self.parameters and not self.formula_latex:
            raise ValueError("I parametri matematici richiedono una formula esplicita")
        return self


class PuntoCriticoGrafico(Contract):
    label: str = Field(min_length=1, max_length=80)
    x: float
    y: float
    description: str = Field(min_length=10, max_length=2000)


class SchedaAnaliticaGrafico(Contract):
    x_axis: AsseGrafico
    y_axis: AsseGrafico
    curves: list[CurvaAnalitica] = Field(min_length=1, max_length=8)
    critical_points: list[PuntoCriticoGrafico] = Field(default_factory=list, max_length=24)
    source_basis: Literal["equation", "tabulated_data", "qualitative"]
    physical_chemical_meaning: str = Field(min_length=20, max_length=6000)
    analytical_steps: list[str] = Field(min_length=1, max_length=20)
    limitations: str = Field(min_length=10, max_length=3000)


class NodoMappa(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    label: str = Field(min_length=1, max_length=90)
    detail: str = Field(default="", max_length=180)
    kind: Literal["concept", "law", "process", "example", "warning"] = "concept"


class ArcoMappa(Contract):
    source: str
    target: str
    label: str = Field(min_length=1, max_length=70)
    kind: Literal["leads_to", "depends_on", "contains", "contrasts", "explains"] = "leads_to"


class SchedaMappa(Contract):
    nodes: list[NodoMappa] = Field(min_length=3, max_length=18)
    edges: list[ArcoMappa] = Field(min_length=2, max_length=30)
    reading_path: list[str] = Field(min_length=2, max_length=10)
    explanation: str = Field(min_length=20, max_length=4000)

    @model_validator(mode="after")
    def valid_graph(self):
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Gli id dei nodi della mappa devono essere unici")
        known = set(node_ids)
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("Ogni collegamento deve riferirsi a nodi esistenti")
        if any(edge.source == edge.target for edge in self.edges):
            raise ValueError("Una mappa non può contenere auto-collegamenti")
        connected = {edge.source for edge in self.edges} | {edge.target for edge in self.edges}
        if connected != known:
            raise ValueError("Ogni nodo della mappa deve partecipare ad almeno un collegamento")
        return self


class SourceVisual(Contract):
    """A source graph represented semantically; no model-provided crop coordinates."""

    title: str = Field(min_length=1, max_length=180)
    kind: Literal["chart", "map"]
    chart: SchedaAnaliticaGrafico | None = None
    concept_map: SchedaMappa | None = None
    uncertainty: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def exactly_one_payload(self):
        if self.kind == "chart" and (self.chart is None or self.concept_map is not None):
            raise ValueError("Una figura chart richiede soltanto la scheda analitica")
        if self.kind == "map" and (self.concept_map is None or self.chart is not None):
            raise ValueError("Una figura map richiede soltanto la scheda mappa")
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


class ConceptNode(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    label: str = Field(min_length=1, max_length=90)
    detail: str = Field(default="", max_length=180)
    kind: Literal["concept", "law", "process", "example", "warning"] = "concept"


class ConceptEdge(Contract):
    source: str
    target: str
    label: str = Field(min_length=1, max_length=70)
    kind: Literal["leads_to", "depends_on", "contains", "contrasts", "explains"] = "leads_to"


class ConceptMap(Contract):
    title: str = Field(min_length=1, max_length=180)
    topic_ids: list[str] = Field(min_length=1)
    nodes: list[ConceptNode] = Field(min_length=3, max_length=14)
    edges: list[ConceptEdge] = Field(min_length=2, max_length=24)
    reading_path: list[str] = Field(min_length=2, max_length=8)
    explanation: str = Field(min_length=20, max_length=3000)

    @model_validator(mode="after")
    def valid_graph(self):
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Gli id dei nodi della mappa devono essere unici")
        known = set(node_ids)
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("Ogni collegamento deve riferirsi a nodi esistenti")
        if any(edge.source == edge.target for edge in self.edges):
            raise ValueError("Una mappa concettuale non può contenere auto-collegamenti")
        connected = {edge.source for edge in self.edges} | {edge.target for edge in self.edges}
        if connected != known:
            raise ValueError("Ogni nodo della mappa deve partecipare ad almeno un collegamento")
        return self


class Lesson(Contract):
    title: str = Field(min_length=1, max_length=180)
    introduction: str = Field(min_length=20)
    sections: list[Section] = Field(min_length=1, max_length=30)
    exercises: list[Exercise] = Field(min_length=1, max_length=20)
    recall: list[Recall] = Field(min_length=3, max_length=20)
    visuals: list[VisualExplanation] = Field(default_factory=list, max_length=60)
    concept_maps: list[ConceptMap] = Field(default_factory=list, max_length=3)
    charts: list[Chart] = Field(default_factory=list, max_length=6)
    recap: list[str] = Field(min_length=2)
    uncertainties: list[str] = Field(default_factory=list)


class TextReplacement(Contract):
    """A narrow repair used after a local LaTeX compiler failure."""
    field_path: str = Field(pattern=r"^/(?:introduction|sections|exercises|recall|visuals|charts|recap|uncertainties)(?:/[A-Za-z0-9_-]+)*$",
                            max_length=300)
    replacement: str = Field(max_length=12000)
    reason: str = Field(min_length=5, max_length=500)


class LessonRepair(Contract):
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacements: list[TextReplacement] = Field(min_length=1, max_length=12)


class LessonPatchOperation(Contract):
    """A bounded RFC-6902-like operation used for scientific review revisions."""
    op: Literal["add", "replace", "remove"]
    path: str = Field(
        pattern=r"^/(?:title|introduction|sections|exercises|recall|visuals|concept_maps|charts|recap|uncertainties)(?:/(?:-|[A-Za-z0-9_-]+))*$",
        max_length=300,
    )
    value: dict | list | str | int | float | bool | None = None
    reason: str = Field(min_length=5, max_length=500)

    @model_validator(mode="after")
    def value_matches_operation(self):
        if self.op in ("add", "replace") and self.value is None:
            raise ValueError("add/replace richiedono value")
        if self.op == "remove" and self.value is not None:
            raise ValueError("remove non accetta value")
        return self


class LessonPatch(Contract):
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[LessonPatchOperation] = Field(min_length=1, max_length=40)


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
