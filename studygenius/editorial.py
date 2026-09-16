"""Deterministic editorial budgets and semantic topic consolidation.

The models decide how to explain; this module decides how much space they may
use.  Keeping those responsibilities separate prevents a request for a short
summary from becoming a merely polite suggestion inside a prompt.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable

from .models import ChapterPlan, Lesson, SourcePage


PROFILE_LABELS = {
    "summary": "Riassunto breve",
    "study": "Dispensa ragionata",
    "transcript": "Sbobina estesa",
}


@dataclass(frozen=True)
class EditorialPolicy:
    profile: str
    label: str
    intent: str
    source_pages: int
    source_words: int
    target_chapters: int
    target_words: int
    hard_max_words: int
    max_topics_per_chapter: int
    max_sections_per_chapter: int
    max_paragraphs_per_chapter: int
    max_exercises_per_chapter: int
    max_recall_per_chapter: int
    max_concept_maps_per_chapter: int
    max_authored_charts_per_chapter: int
    allow_generated_exercises: bool
    require_concept_map: bool
    append_recall_answers: bool

    def prompt(self) -> dict:
        return asdict(self)


def _words(value: str) -> int:
    return len(re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+", value))


def make_editorial_policy(profile: str, pages: list[SourcePage], topic_count: int) -> EditorialPolicy:
    """Derive a scalable budget from source density rather than a fixed multiplier."""
    if profile not in PROFILE_LABELS:
        raise ValueError("Profilo editoriale non supportato")
    page_count = max(1, len(pages))
    source_words = sum(_words(page.text) for page in pages)
    density = source_words / page_count
    if profile == "summary":
        # Slides need explanatory expansion; dense books need aggressive compression.
        words_per_page = min(280, max(90, 320 - 0.55 * density))
        target_chapters = max(math.ceil(page_count / 6), math.ceil(topic_count / 24))
        values = dict(
            intent=("Selezionare e collegare le idee indispensabili. Ogni concetto si spiega una sola "
                    "volta; esempi, esercizi e visuali entrano soltanto se chiariscono un nodo centrale."),
            hard_factor=1.30, chapter_cap=50, topics=32, sections=6, paragraphs=14,
            exercises=1, recall=0, maps=1, charts=1, generated=False,
            require_map=False, appendix=False,
        )
    elif profile == "study":
        words_per_page = min(520, max(180, 600 - 0.70 * density))
        target_chapters = max(math.ceil(page_count / 4), math.ceil(topic_count / 10))
        values = dict(
            intent=("Costruire una dispensa autosufficiente ma selettiva: spiegazioni complete sui nuclei "
                    "importanti, esempi mirati e nessuna ripetizione tra capitoli."),
            hard_factor=1.40, chapter_cap=80, topics=18, sections=10, paragraphs=26,
            exercises=2, recall=4, maps=1, charts=2, generated=True,
            require_map=False, appendix=True,
        )
    else:
        words_per_page = min(900, max(350, 1000 - 0.80 * density))
        target_chapters = max(math.ceil(page_count / 2), math.ceil(topic_count / 7))
        values = dict(
            intent=("Conservare il percorso quasi completo della fonte, inclusi passaggi, esempi ed esercizi, "
                    "senza trascrivere ridondanze o ripetere lo stesso concetto in capitoli diversi."),
            hard_factor=1.50, chapter_cap=150, topics=12, sections=18, paragraphs=48,
            exercises=5, recall=4, maps=2, charts=4, generated=True,
            require_map=True, appendix=True,
        )
    target_chapters = min(values["chapter_cap"], max(1, target_chapters))
    target_floor = {"summary": 700, "study": 1800, "transcript": 2500}[profile]
    hard_floor = {"summary": 950, "study": 2600, "transcript": 4000}[profile]
    target_words = max(target_floor, target_chapters * 250,
                       round(page_count * words_per_page))
    hard_max_words = max(hard_floor, target_words + 250,
                         target_chapters * 350,
                         math.ceil(target_words * values["hard_factor"]))
    return EditorialPolicy(
        profile=profile, label=PROFILE_LABELS[profile], intent=values["intent"],
        source_pages=page_count, source_words=source_words,
        target_chapters=target_chapters, target_words=target_words,
        hard_max_words=hard_max_words, max_topics_per_chapter=values["topics"],
        max_sections_per_chapter=values["sections"],
        max_paragraphs_per_chapter=values["paragraphs"],
        max_exercises_per_chapter=values["exercises"],
        max_recall_per_chapter=values["recall"],
        max_concept_maps_per_chapter=values["maps"],
        max_authored_charts_per_chapter=values["charts"],
        allow_generated_exercises=values["generated"],
        require_concept_map=values["require_map"],
        append_recall_answers=values["appendix"],
    )


_STOPWORDS = {
    "a", "ad", "ai", "al", "alla", "alle", "con", "da", "dei", "del", "della", "delle",
    "di", "e", "gli", "i", "il", "in", "la", "le", "lo", "nel", "nella", "o", "per",
    "su", "tra", "un", "una", "verso",
}


def _title_tokens(title: str) -> frozenset[str]:
    plain = unicodedata.normalize("NFKD", title.casefold()).encode("ascii", "ignore").decode()
    return frozenset(token for token in re.findall(r"[a-z0-9]+", plain)
                     if token not in _STOPWORDS and len(token) > 1)


def cluster_topic_inventory(topics: dict[str, dict]) -> tuple[list[dict], dict[str, list[str]]]:
    """Group repeated/near-identical headings while retaining every source topic id."""
    clusters: list[dict] = []
    for topic_id, topic in topics.items():
        tokens = _title_tokens(topic["title"])
        owner = None
        for cluster in clusters:
            known = cluster["_tokens"]
            union = tokens | known
            similarity = len(tokens & known) / len(union) if union else 1.0
            contained = min(len(tokens), len(known)) >= 2 and (tokens <= known or known <= tokens)
            if tokens == known or contained or (len(tokens) >= 3 and len(known) >= 3 and similarity >= 0.82):
                owner = cluster
                break
        if owner is None:
            # Reuse the first real topic id as the unit id. This keeps checkpoints
            # and replay fixtures compatible while the member list records every
            # repeated occurrence that must be explained together.
            owner = {"id": topic_id, "title": topic["title"],
                     "kind": topic["kind"], "topic_ids": [], "_tokens": tokens}
            clusters.append(owner)
        owner["topic_ids"].append(topic_id)
    units = [{key: value for key, value in cluster.items() if key != "_tokens"} for cluster in clusters]
    return units, {unit["id"]: unit["topic_ids"] for unit in units}


def select_visuals(visuals: dict[str, dict], profile: str) -> dict[str, dict]:
    """Project the complete visual evidence layer into the chosen publication profile."""
    if profile != "summary":
        return visuals
    return {key: value for key, value in visuals.items() if value.get("importance") == "essential"}


def allocate_chapter_budgets(policy: EditorialPolicy, plans: list[ChapterPlan]) -> list[dict]:
    if not plans:
        return []
    weights = [max(1, len(plan.topic_ids)) for plan in plans]
    weight_total = sum(weights)
    minimum = 350
    hard_total = max(policy.hard_max_words, minimum * len(plans))
    target_total = max(policy.target_words, 250 * len(plans))
    budgets = []
    hard_remaining = hard_total - minimum * len(plans)
    target_minimum = 250
    target_remaining = target_total - target_minimum * len(plans)
    hard_extra = [math.floor(hard_remaining * weight / weight_total) for weight in weights]
    target_extra = [math.floor(target_remaining * weight / weight_total) for weight in weights]
    for index in range(hard_remaining - sum(hard_extra)):
        hard_extra[index % len(plans)] += 1
    for index in range(target_remaining - sum(target_extra)):
        target_extra[index % len(plans)] += 1
    for index, weight in enumerate(weights):
        hard_words = minimum + hard_extra[index]
        target_words = target_minimum + target_extra[index]
        budgets.append({
            "profile": policy.profile, "label": policy.label, "intent": policy.intent,
            "target_words": min(target_words, hard_words), "hard_max_words": hard_words,
            "max_sections": policy.max_sections_per_chapter,
            "max_paragraphs": policy.max_paragraphs_per_chapter,
            "max_exercises": policy.max_exercises_per_chapter,
            "max_recall": policy.max_recall_per_chapter,
            "max_concept_maps": policy.max_concept_maps_per_chapter,
            "max_authored_charts": policy.max_authored_charts_per_chapter,
            "allow_generated_exercises": policy.allow_generated_exercises,
            "require_concept_map": policy.require_concept_map,
        })
    return budgets


_NON_PROSE_KEYS = {"topic_ids", "visual_id", "id", "source", "target", "origin", "kind", "style"}


def lesson_word_count(lesson: Lesson) -> int:
    def walk(value, key="") -> Iterable[str]:
        if isinstance(value, str):
            if key not in _NON_PROSE_KEYS:
                yield value
        elif isinstance(value, dict):
            for child_key, child in value.items():
                yield from walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child, key)
    return sum(_words(text) for text in walk(lesson.model_dump(mode="json")))


def validate_lesson_budget(lesson: Lesson, budget: dict, topic_ids: list[str]) -> None:
    words = lesson_word_count(lesson)
    if words > budget["hard_max_words"]:
        raise ValueError(
            f"Capitolo troppo lungo: {words} parole; limite editoriale {budget['hard_max_words']}. "
            "Comprimi, unifica le ripetizioni e conserva formule e passaggi indispensabili."
        )
    if len(lesson.sections) > budget["max_sections"]:
        raise ValueError("Troppe sezioni per il profilo scelto: unisci gli argomenti affini")
    paragraphs = sum(len(section.paragraphs) for section in lesson.sections)
    if paragraphs > budget["max_paragraphs"]:
        raise ValueError("Troppi paragrafi: elimina ripetizioni e usa una progressione più compatta")
    for field, maximum in (("exercises", "max_exercises"), ("recall", "max_recall"),
                           ("concept_maps", "max_concept_maps"),
                           ("charts", "max_authored_charts")):
        if len(getattr(lesson, field)) > budget[maximum]:
            raise ValueError(f"Il profilo editoriale ammette al massimo {budget[maximum]} elementi in {field}")
    if not budget["allow_generated_exercises"] and any(item.origin == "generated" for item in lesson.exercises):
        raise ValueError("Il riassunto breve non ammette esercizi inventati")
    if budget["require_concept_map"] and len(topic_ids) >= 2 and not lesson.concept_maps:
        raise ValueError("Il profilo esteso richiede una mappa per un capitolo con più argomenti")


def validate_book_budget(lessons: list[Lesson], policy: EditorialPolicy) -> dict:
    generated_words = sum(lesson_word_count(lesson) for lesson in lessons)
    if generated_words > policy.hard_max_words:
        raise ValueError(
            f"La dispensa supera il limite editoriale globale: {generated_words} parole su "
            f"{policy.hard_max_words}."
        )
    return {
        **policy.prompt(), "generated_words": generated_words,
        "word_expansion_ratio": round(generated_words / max(1, policy.source_words), 2),
    }
