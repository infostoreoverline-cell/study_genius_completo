from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

from . import prompts
from .config import Settings
from .editorial import (allocate_chapter_budgets, cluster_topic_inventory,
                        make_editorial_policy, select_visuals,
                        validate_book_budget, validate_lesson_budget)
from .ingest import ingest, render_high_fidelity_page
from .models import (ChapterPlan, CourseGuide, EvidenceBatch, JobOptions, Lesson,
                     LessonPatch, LessonRepair, Outline, PageAnalysis, Review)
from .providers import Models
from .render import (LatexError, build_book, create_layout_review_assets,
                     inspect_pdf_layout, render_charts, render_concept_maps, render_source_visual, source_archive,
                     validate_lesson_math, preflight_latex)
from .storage import Store, atomic_json, read_json
from .vision import needs_high_fidelity


class Paused(RuntimeError):
    pass


def validate_evidence(batch: EvidenceBatch, expected: list[str]):
    if Counter(p.page_id for p in batch.pages) != Counter(expected):
        raise ValueError("Le pagine restituite devono coincidere esattamente con " + ", ".join(expected))


def validate_outline(outline: Outline, expected: list[str]):
    actual = [t for c in outline.chapters for t in c.topic_ids]
    if Counter(actual) != Counter(expected):
        missing = set(expected) - set(actual)
        extra = set(actual) - set(expected)
        repeated = [t for t, n in Counter(actual).items() if n > 1]
        raise ValueError(f"Copertura indice non valida. Mancanti: {sorted(missing)}; sconosciuti: {sorted(extra)}; ripetuti: {repeated}")


def validate_outline_budget(outline: Outline, expected: list[str], max_chapters: int):
    validate_outline(outline, expected)
    if len(outline.chapters) > max_chapters:
        raise ValueError(
            f"Indice troppo frammentato: {len(outline.chapters)} capitoli; il profilo ne ammette "
            f"al massimo {max_chapters} in questo blocco. Unisci i nuclei affini."
        )


def chapter_pipeline_limit(settings: Settings, chapter_count: int) -> int:
    """Keep both providers busy without raising either provider's API limit.

    A chapter alternates DeepSeek, local compilation and Gemini. Limiting the
    whole chapter to DeepSeek's concurrency leaves capacity idle in the other
    phases. Provider-specific semaphores in ``Models`` remain authoritative.
    """
    return min(chapter_count, 8, settings.deepseek_concurrency + settings.gemini_concurrency)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes} min {remainder:02d} s"


def validate_lesson(lesson: Lesson, topic_ids: list[str], visual_ids: list[str]):
    expected = Counter(topic_ids)
    actual = Counter(t for s in lesson.sections for t in s.topic_ids)
    if actual != expected:
        raise ValueError(f"La lezione deve trattare esattamente una volta questi topic_ids: {topic_ids}")
    known = set(expected)
    for item in [*lesson.exercises, *lesson.charts, *lesson.concept_maps]:
        if not set(item.topic_ids) <= known:
            raise ValueError("Esercizio o visuale con riferimenti a fonti inesistenti nel capitolo")
    if Counter(v.visual_id for v in lesson.visuals) != Counter(visual_ids):
        raise ValueError(f"Spiegare esattamente una volta ciascun visual_id: {visual_ids}")
    validate_lesson_math(lesson)


def validate_chapter_lesson(lesson: Lesson, topic_ids, visual_ids, known_visual_ids, budget=None):
    # Other known figures already have an owner in the course plan. Ignore those
    # extra explanations instead of regenerating an otherwise valid chapter.
    # Missing assigned figures, duplicates and unknown IDs still fail below.
    assigned, known = set(visual_ids), set(known_visual_ids)
    lesson.visuals = [v for v in lesson.visuals if v.visual_id in assigned or v.visual_id not in known]
    validate_lesson(lesson, topic_ids, visual_ids)
    if budget is not None:
        validate_lesson_budget(lesson, budget, topic_ids)
    elif len(topic_ids) >= 2 and not lesson.concept_maps:
        raise ValueError("Un capitolo con più argomenti deve includere almeno una mappa concettuale")


def bounded_groups(items, max_items, max_chars, key=lambda x: x):
    group, size = [], 0
    for item in items:
        length = len(json.dumps(key(item), ensure_ascii=False))
        if length > max_chars:
            raise ValueError("Un singolo argomento supera il limite di contesto: dividere il PDF o la pagina.")
        if group and (len(group) >= max_items or size + length > max_chars):
            yield group
            group, size = [], 0
        group.append(item)
        size += length
    if group:
        yield group


def compact_json(value) -> str:
    """Stable JSON for prompts: fewer tokens and repeatable provider cache prefixes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def adaptive_page_groups(pages, vision_manifest: dict, max_pages: int, max_chars: int = 140000):
    """Batch simple digital pages densely and isolate visually expensive pages.

    ``pages_per_batch`` remains a hard ceiling. Scan/small-text pages consume two
    capacity units because combining four of them tends to slow vision inference
    and increases the chance of a paid high-resolution reread.
    """
    metadata = vision_manifest.get("pages", {}) if isinstance(vision_manifest, dict) else {}
    risky = {"scan", "small-text", "fallback"}
    group, chars, units = [], 0, 0
    for page in pages:
        profile = metadata.get(page.id, {}).get("profile", "technical")
        page_units = 2 if profile in risky else 1
        length = len(compact_json(page.model_dump(mode="json")))
        if length > max_chars:
            raise ValueError("Un singolo argomento supera il limite di contesto: dividere il PDF o la pagina.")
        if group and (len(group) >= max_pages or units + page_units > max_pages or chars + length > max_chars):
            yield group
            group, chars, units = [], 0, 0
        group.append(page)
        chars += length
        units += page_units
    if group:
        yield group


def source_visual_catalog(visuals, max_chars=60000):
    """Keep source presence distinct from the images assigned to one chapter."""
    entries, size = [], 0
    for visual_id, visual in visuals.items():
        entry = {"visual_id": visual_id, "page_id": visual["page_id"], "title": visual["title"]}
        length = len(json.dumps(entry, ensure_ascii=False))
        if size + length > max_chars:
            break
        entries.append(entry)
        size += length
    return {"entries": entries, "complete": len(entries) == len(visuals), "total": len(visuals)}


def source_page_context(topics, assigned_ids, max_chars=60000):
    """Supply tables/givens on an exercise's pages without assigning extra topics."""
    pages = {topics[t]["page_id"] for t in assigned_ids}
    supporting = [(t, value) for t, value in topics.items()
                  if t not in assigned_ids and value["page_id"] in pages]
    entries, size = {}, 0
    for topic_id, value in supporting:
        length = len(json.dumps({topic_id: value}, ensure_ascii=False))
        if size + length > max_chars:
            break
        entries[topic_id] = value
        size += length
    return {"entries": entries, "complete": len(entries) == len(supporting), "total": len(supporting)}


def lesson_sha256(lesson: Lesson) -> str:
    canonical = json.dumps(lesson.model_dump(mode="json"), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def apply_lesson_repair(lesson: Lesson, repair: LessonRepair) -> Lesson:
    """Apply compiler repairs only to existing string leaves, then revalidate fully."""
    expected = lesson_sha256(lesson)
    if repair.base_sha256 != expected:
        raise ValueError("La riparazione LaTeX non corrisponde alla versione inviata")
    value = lesson.model_dump(mode="json")
    seen = set()
    for replacement in repair.replacements:
        if replacement.field_path in seen:
            raise ValueError("La riparazione LaTeX ripete lo stesso campo")
        seen.add(replacement.field_path)
        parts = [part.replace("~1", "/").replace("~0", "~")
                 for part in replacement.field_path.removeprefix("/").split("/")]
        node = value
        for part in parts[:-1]:
            if isinstance(node, list):
                if not part.isdigit() or int(part) >= len(node):
                    raise ValueError("Percorso di riparazione LaTeX non valido")
                node = node[int(part)]
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise ValueError("Percorso di riparazione LaTeX non valido")
        leaf = parts[-1]
        if isinstance(node, list):
            if not leaf.isdigit() or int(leaf) >= len(node) or not isinstance(node[int(leaf)], str):
                raise ValueError("La riparazione deve sostituire un campo testuale esistente")
            node[int(leaf)] = replacement.replacement
        elif isinstance(node, dict) and leaf in node and isinstance(node[leaf], str):
            node[leaf] = replacement.replacement
        else:
            raise ValueError("La riparazione deve sostituire un campo testuale esistente")
    return Lesson.model_validate(value)


def _pointer_parts(path: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~")
            for part in path.removeprefix("/").split("/")]


def apply_lesson_patch(lesson: Lesson, patch: LessonPatch) -> Lesson:
    """Apply a small review delta, then let the full Lesson contract revalidate it."""
    if patch.base_sha256 != lesson_sha256(lesson):
        raise ValueError("La revisione non corrisponde alla versione del capitolo inviata")
    value = lesson.model_dump(mode="json")
    seen = set()
    for operation in patch.operations:
        if operation.path in seen:
            raise ValueError("La revisione ripete lo stesso percorso")
        seen.add(operation.path)
        parts = _pointer_parts(operation.path)
        node = value
        for part in parts[:-1]:
            if isinstance(node, list):
                if not part.isdigit() or int(part) >= len(node):
                    raise ValueError("Percorso di revisione non valido")
                node = node[int(part)]
            elif isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise ValueError("Percorso di revisione non valido")
        leaf = parts[-1]
        replacement = copy.deepcopy(operation.value)
        if isinstance(node, list):
            if operation.op == "add":
                if leaf == "-":
                    node.append(replacement)
                elif leaf.isdigit() and int(leaf) <= len(node):
                    node.insert(int(leaf), replacement)
                else:
                    raise ValueError("Indice di aggiunta non valido")
            elif leaf.isdigit() and int(leaf) < len(node):
                if operation.op == "replace":
                    node[int(leaf)] = replacement
                else:
                    node.pop(int(leaf))
            else:
                raise ValueError("Indice di revisione non valido")
        elif isinstance(node, dict):
            if operation.op == "add":
                node[leaf] = replacement
            elif leaf not in node:
                raise ValueError("Percorso di revisione non valido")
            elif operation.op == "replace":
                node[leaf] = replacement
            else:
                del node[leaf]
        else:
            raise ValueError("Percorso di revisione non valido")
    return Lesson.model_validate(value)


def chapter_model_tier(plan: ChapterPlan, topics: dict) -> str:
    """Reserve the reasoning model for chapters with actual mathematical complexity."""
    assigned = [topics[topic_id] for topic_id in plan.topic_ids]
    if any(topic.get("kind") in ("derivation", "exercise") for topic in assigned):
        return "quality"
    scientific_marks = sum(len(re.findall(r"[=\u221a\u222b\u2211\u2202_^]|\\(?:frac|int|sum|sqrt)",
                                           topic.get("content", ""))) for topic in assigned)
    return "quality" if scientific_marks >= 8 else "fast"


class Pipeline:
    def __init__(self, store: Store, job_id: str, settings: Settings, stop: asyncio.Event):
        self.store, self.job_id, self.settings, self.stop = store, job_id, settings, stop
        self.directory = store.directory(job_id)
        self.options = JobOptions.model_validate(store.get(job_id)["options"])
        self.models = Models(settings, store, job_id, self.check) if self.options.mode == "live" else None

    def check(self):
        if self.stop.is_set():
            raise Paused("Pausa richiesta. I passaggi già completati sono salvati.")

    def progress(self, stage, progress, message=None):
        self.check()
        self.store.update(self.job_id, stage=stage, progress=progress)
        if message:
            self.store.event(self.job_id, message)

    async def parallel_map(self, items, limit, worker, on_complete=None):
        """Run a large collection with only ``limit`` live coroutines."""
        items = list(items)
        if not items:
            return []
        queue = asyncio.Queue()
        for entry in enumerate(items):
            queue.put_nowait(entry)
        ordered = [None] * len(items)
        completed = 0
        lock = asyncio.Lock()

        async def consume():
            nonlocal completed
            while True:
                try:
                    index, item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                self.check()
                ordered[index] = await worker(index, item)
                async with lock:
                    completed += 1
                    if on_complete:
                        on_complete(completed, len(items))

        tasks = [asyncio.create_task(consume()) for _ in range(min(max(1, limit), len(items)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return ordered

    async def run(self):
        try:
            await self._run()
        finally:
            if self.models:
                await self.models.close()

    async def _run(self):
        self.progress("Lettura dei PDF", 0.02, "Verifico e indicizzo ogni pagina dei documenti.")
        if self.options.mode == "demo":
            from .demo import run_demo
            await run_demo(self)
            return
        if not self.settings.gemini_key.get_secret_value() or not self.settings.deepseek_key.get_secret_value():
            raise ValueError("Configura entrambe le chiavi API prima di avviare il lavoro.")
        self.progress("Verifica dell'installazione LaTeX", 0.01)
        await asyncio.to_thread(preflight_latex, self.directory / "preflight")
        self.check()
        pages = await asyncio.to_thread(ingest, self.directory, self.check,
                                       lambda a, b: self.progress(f"Indicizzazione {a}/{b}", 0.02 + 0.06*a/b))
        evidence = []
        vision_manifest = read_json(self.directory / "vision.json") if (self.directory / "vision.json").exists() else {}
        page_groups = list(adaptive_page_groups(pages, vision_manifest, self.options.pages_per_batch))
        self.store.event(self.job_id, f"{len(pages)} pagine, {len(page_groups)} blocchi di lettura visiva. Nessuna pagina viene saltata.")

        async def read_batch(index, batch):
            checkpoint = self.directory / "evidence" / f"batch-{index:04d}.json"
            expected = [p.id for p in batch]
            result = None
            if checkpoint.exists():
                try:
                    result = EvidenceBatch.model_validate(read_json(checkpoint))
                    validate_evidence(result, expected)
                except (ValueError, KeyError):
                    checkpoint.unlink(missing_ok=True)
                    self.store.event(self.job_id,
                        f"Lettura blocco {index+1}: checkpoint visivo precedente incompatibile; lo rigenero.")
            if result is None:
                result = await self.models.json("gemini", f"Lettura blocco {index+1}", prompts.READ,
                    compact_json({"pages": [{"page_id": p.id, "filename": p.filename, "page": p.number, "text": p.text} for p in batch]}),
                    EvidenceBatch, images=[self.directory / p.image for p in batch],
                    validate=lambda r: validate_evidence(r, expected), max_output=24000)
                # Most pages use fewer pixels. If the reader explicitly reports a
                # readability problem, only that page is rerendered and reread larger.
                replacements = {}
                by_page = {analysis.page_id: analysis for analysis in result.pages}
                uncertain = []
                for page in batch:
                    analysis = by_page[page.id]
                    doubts = [*analysis.uncertainties,
                              *(visual.uncertainty for visual in analysis.visuals if visual.uncertainty)]
                    if needs_high_fidelity(doubts):
                        uncertain.append((page, analysis))

                async def reread_page(_, item):
                    page, analysis = item
                    high = await asyncio.to_thread(render_high_fidelity_page, self.directory, page)
                    reread = await self.models.json("gemini", f"Rilettura alta definizione {page.id}", prompts.READ,
                        compact_json({"pages": [{"page_id": page.id, "filename": page.filename,
                                                 "page": page.number, "text": page.text}],
                                      "previous_analysis": analysis.model_dump(),
                                      "focus": "Rileggi soltanto etichette, simboli e valori rimasti poco leggibili."}),
                        EvidenceBatch, images=[high],
                        validate=lambda r, page_id=page.id: validate_evidence(r, [page_id]), max_output=24000)
                    return page.id, reread.pages[0]

                if uncertain:
                    replacements.update(dict(await self.parallel_map(
                        uncertain, min(len(uncertain), self.settings.gemini_concurrency), reread_page)))
                if replacements:
                    result = EvidenceBatch(pages=[replacements.get(page.page_id, page) for page in result.pages])
                atomic_json(checkpoint, result.model_dump())
            return result

        self.progress("Lettura multimodale parallela", 0.08)
        batches = await self.parallel_map(page_groups, self.settings.gemini_concurrency, read_batch,
            lambda done, total: self.progress(f"Gemini legge le pagine · {done}/{total}", 0.08 + 0.22*done/total))
        for result in batches:
            evidence.extend(result.pages)
        # Restore original page order even when the model returned pages in a different order.
        order = {p.id: i for i, p in enumerate(pages)}
        evidence.sort(key=lambda p: order[p.page_id])
        atomic_json(self.directory / "evidence.json", [p.model_dump() for p in evidence])
        topics, detected_visuals = {}, {}
        for page in evidence:
            for i, topic in enumerate(page.topics, 1):
                topics[f"{page.page_id}-T{i:02d}"] = {"page_id": page.page_id, **topic.model_dump()}
            for i, visual in enumerate(page.visuals, 1):
                detected_visuals[f"{page.page_id}-V{i:02d}"] = {"page_id": page.page_id, **visual.model_dump()}
        if not topics:
            raise ValueError("Nessun contenuto didattico leggibile: controlla i PDF e le pagine escluse in evidence.json.")
        self.editorial_policy = make_editorial_policy(
            self.options.output_profile, pages, len(topics))
        self.detected_visuals = detected_visuals
        atomic_json(self.directory / "editorial-policy.json", self.editorial_policy.prompt())
        visuals = select_visuals(detected_visuals, self.options.output_profile)
        self.progress("Costruzione del percorso di studio", 0.31,
            f"Individuati {len(topics)} argomenti. Profilo {self.editorial_policy.label}: "
            f"circa {self.editorial_policy.target_chapters} capitoli e {self.editorial_policy.target_words} parole; "
            f"{len(visuals)}/{len(detected_visuals)} figure selezionate.")
        outline_file = self.directory / "outline.json"
        if outline_file.exists():
            plans = [ChapterPlan.model_validate(x) for x in read_json(outline_file)]
        else:
            plans = []
            units, unit_members = cluster_topic_inventory(topics)
            inventories = [{"id": unit["id"], "title": unit["title"], "kind": unit["kind"],
                            "occurrences": len(unit["topic_ids"])} for unit in units]
            inventory_groups = list(bounded_groups(inventories, 200, 100000))

            async def plan_batch(index, batch):
                batch_target = max(1, round(self.editorial_policy.target_chapters * len(batch) / len(inventories)))
                return await self.models.json("deepseek", f"Indice {index+1}", prompts.PLAN,
                    compact_json({"exam_brief": self.options.exam_brief,
                                  "editorial_policy": {**self.editorial_policy.prompt(),
                                                       "target_chapters_for_this_batch": batch_target},
                                  "topics": batch}), Outline,
                    validate=lambda r, expected=[t["id"] for t in batch], maximum=batch_target:
                        validate_outline_budget(r, expected, maximum), tier="fast")

            outlines = await self.parallel_map(inventory_groups, self.settings.deepseek_concurrency, plan_batch)
            for outline in outlines:
                for plan in outline.chapters:
                    expanded_ids = [topic_id for unit_id in plan.topic_ids for topic_id in unit_members[unit_id]]
                    # Bound writing context independently of how the planner groups semantic units.
                    groups = list(bounded_groups(expanded_ids,
                        self.editorial_policy.max_topics_per_chapter, 90000, lambda t: topics[t]))
                    for n, group in enumerate(groups, 1):
                        title = plan.title + (f" - Parte {n}" if len(groups) > 1 else "")
                        plans.append(ChapterPlan(title=title[:180], topic_ids=group,
                                                 objectives=plan.objectives, prerequisites=plan.prerequisites))
            atomic_json(outline_file, [p.model_dump() for p in plans])
        if Counter(t for p in plans for t in p.topic_ids) != Counter(topics.keys()):
            raise ValueError("Il checkpoint dell'indice non copre gli argomenti estratti")
        self.chapter_budgets = allocate_chapter_budgets(self.editorial_policy, plans)
        topic_units, _ = cluster_topic_inventory(topics)
        self.topic_clusters = topic_units
        self.progress("Allineamento delle convenzioni tra capitoli", 0.33)
        guide_file = self.directory / "course-guide.json"
        if guide_file.exists():
            self.course_guide = CourseGuide.model_validate(read_json(guide_file))
        else:
            guide_groups = list(bounded_groups(list(topics.items()), 100, 80000))

            async def guide_batch(index, batch):
                return await self.models.json("gemini", "Convenzioni del corso", prompts.GUIDE,
                    compact_json(dict(batch)), CourseGuide, max_output=16000)

            guides = await self.parallel_map(guide_groups, self.settings.gemini_concurrency, guide_batch)
            merged = {key: list(dict.fromkeys(item for guide in guides for item in getattr(guide, key)))
                      for key in ("conventions", "symbols", "conflicts")}
            if len(json.dumps(merged)) > 40000 or len(guides) > 1:
                # Merge compact extraction summaries, preserving conflicts explicitly.
                if len(json.dumps(merged)) > 180000:
                    raise ValueError("Le convenzioni del corso superano il contesto: separa i corsi in progetti distinti.")
                self.course_guide = await self.models.json("gemini", "Sintesi delle convenzioni", prompts.GUIDE,
                    compact_json(merged), CourseGuide, max_output=10000)
            else:
                self.course_guide = CourseGuide.model_validate(merged)
            atomic_json(guide_file, self.course_guide.model_dump())
        guide_digest = hashlib.sha256(self.course_guide.model_dump_json().encode()).hexdigest()
        self.course_outline = [{"title": p.title, "topic_ids": p.topic_ids} for p in plans]
        page_map = {p.id: p for p in pages}
        refs = {t: f"{page_map[v['page_id']].document}, p. {page_map[v['page_id']].number}" for t, v in topics.items()}
        visual_assignments = {i: [] for i in range(len(plans))}
        for visual_id, value in visuals.items():
            owner = next(i for i, plan in enumerate(plans) if any(topics[t]["page_id"] == value["page_id"] for t in plan.topic_ids))
            visual_assignments[owner].append(visual_id)
        assets = {}
        from .models import SourceVisual

        async def prepare_visual(index, item):
            visual_id, value = item
            page = page_map[value["page_id"]]
            visual = SourceVisual.model_validate({k: v for k, v in value.items() if k != "page_id"})
            asset = await asyncio.to_thread(
                render_source_visual, visual, self.directory / "visuals", visual_id)
            return visual_id, {**asset, "title": visual.title,
                               "reference": f"{page.document}, p. {page.number}"}

        prepared = await self.parallel_map(list(visuals.items()), min(4, max(1, len(visuals))), prepare_visual)
        assets.update(dict(prepared))
        documents = read_json(self.directory / "inputs.json")

        async def produce_chapter(index, plan):
            started = time.perf_counter()
            phase_times = Counter()
            self.store.event(self.job_id, f"Avvio capitolo {index+1}/{len(plans)}: {plan.title}")
            chapter_dir = self.directory / "chapters" / f"C{index+1:03d}"
            chapter_dir.mkdir(parents=True, exist_ok=True)
            final = chapter_dir / "final.json"
            visual_ids = visual_assignments[index]
            editorial_budget = self.chapter_budgets[index]
            saved = read_json(final) if final.exists() else None
            if saved and saved.get("guide_digest") == guide_digest:
                lesson, review = Lesson.model_validate(saved["lesson"]), Review.model_validate(saved["review"])
                validate_chapter_lesson(lesson, plan.topic_ids, visual_ids, visuals, editorial_budget)
                stored_timing = saved.get("timing")
                timing = (stored_timing if isinstance(stored_timing, dict)
                          else {"resumed": True, "phase_seconds": {}})
                timing["resumed"] = True
            else:
                lesson, review = await self.chapter(index, plan, chapter_dir, topics, visuals, visual_ids,
                                                    page_map, assets, refs, documents, phase_times,
                                                    editorial_budget)
                timing = {"resumed": False, "phase_seconds": {key: round(value, 2)
                          for key, value in phase_times.items()}}
                atomic_json(final, {"lesson": lesson.model_dump(), "review": review.model_dump(),
                                    "guide_digest": guide_digest, "timing": timing})
            timing = {**timing, "wall_seconds": round(time.perf_counter() - started, 2)}
            self.store.event(self.job_id,
                f"Capitolo {index+1}/{len(plans)} completato in {format_duration(timing['wall_seconds'])}"
                + (" (ripreso dai checkpoint)." if timing.get("resumed") else "."))
            return lesson, review, timing

        self.progress("Scrittura e revisione parallela dei capitoli", 0.34)
        chapter_concurrency = chapter_pipeline_limit(self.settings, len(plans))
        self.store.event(self.job_id,
            f"Mantengo attivi fino a {chapter_concurrency} capitoli indipendenti; i limiti API restano "
            f"Gemini {self.settings.gemini_concurrency} e DeepSeek {self.settings.deepseek_concurrency}.")
        chapter_results = await self.parallel_map(plans, chapter_concurrency, produce_chapter,
            lambda done, total: self.progress(f"Capitoli completati · {done}/{total}", 0.34 + 0.46*done/total))
        lessons = [result[0] for result in chapter_results]
        reviews = [result[1] for result in chapter_results]
        chapter_timings = [result[2] for result in chapter_results]
        editorial_metrics = validate_book_budget(lessons, self.editorial_policy)
        self.progress("Verifica del programma d'esame", 0.81)
        audit_reviews = []
        # Hierarchical audit: an exhaustive inventory is provided in bounded batches. A final
        # syllabus audit uses all chapter titles/objectives without the lengthy source text.
        def coverage_evidence(lesson):
            candidates = [paragraph for section in lesson.sections for paragraph in section.paragraphs]
            pattern = re.compile(r"piston|cilindr|sorgent|diaterm|adiabatic|isolament|paret", re.IGNORECASE)
            return [text[:700] for text in candidates if pattern.search(text)][:10]

        # Section titles and short supporting excerpts let the audit judge what
        # was actually written. Auditing only planner topic names produced false
        # omissions and then caused needless rewrite/review cycles.
        catalog = [{"chapter": plan.title, "objectives": plan.objectives,
                    "topics": [topics[t]["title"] for t in plan.topic_ids],
                    "section_titles": [section.title for section in lesson.sections],
                    "recap": lesson.recap,
                    "supporting_excerpts": coverage_evidence(lesson)}
                   for plan, lesson in zip(plans, lessons)]
        catalog_text = json.dumps(catalog, ensure_ascii=False)
        if len(catalog_text) <= 180000:
            appendices = ["Fonti e tracciabilità dei documenti caricati."]
            if self.editorial_policy.append_recall_answers:
                appendices.insert(0,
                    "Risposte al richiamo attivo: ogni domanda dei capitoli è seguita dalla sua risposta motivata.")
            audit = await self.models.json("gemini", "Copertura del programma", prompts.AUDIT,
                compact_json({"exam_brief": self.options.exam_brief,
                            "editorial_policy": self.editorial_policy.prompt(), "catalog": catalog,
                            "document_structure": {"appendices": appendices}}), Review)
            audit_reviews.append(audit.model_dump())
        else:
            audit_reviews.append({"passed": False, "issues": [{"severity": "major", "message":
                "Indice molto esteso: confronto globale con il programma non eseguito. Verificare la copertura manualmente."}]})
        report = self.report(pages, evidence, topics, visuals, plans, lessons, reviews, audit_reviews)
        report["editorial"] = editorial_metrics
        report["visual_projection"] = {"detected": len(detected_visuals),
                                       "selected": len(visuals)}
        report["performance"] = {"chapter_pipeline_concurrency": chapter_concurrency,
                                 "provider_limits_unchanged": True,
                                 "chapters": chapter_timings}
        report["course_guide"] = self.course_guide.model_dump()
        report["issues"].extend(self.course_guide.conflicts)
        output = self.directory / "output"
        printed_issues = list(report["issues"])
        self.progress("Composizione e compilazione LaTeX", 0.86, "Creo il PDF con testo selezionabile, formule e figure.")
        diagnostics = await asyncio.to_thread(build_book, output, self.options.title, [p.model_dump() for p in plans],
            lessons, assets, refs, documents, report, self.options.mode, self.options.output_profile)
        report["layout"] = diagnostics
        self.add_layout_issues(report, diagnostics)
        self.check()
        # The compiler and a local geometry pass inspect every page. Gemini receives
        # compact all-page contact sheets, then full-size copies of pages with figures,
        # small type or structural boundaries. This retains complete coverage without
        # paying for a high-resolution multimodal call for every ordinary text page.
        layout_scan = await asyncio.to_thread(inspect_pdf_layout, output / "dispensa.pdf")
        report["local_layout_scan"] = layout_scan
        report["issues"].extend(layout_scan["issues"])
        review_assets = await asyncio.to_thread(create_layout_review_assets, output / "dispensa.pdf",
                                                self.directory / "layout", layout_scan["detailed_pages"])
        review_jobs = []
        for batch in bounded_groups(review_assets["overview"], 4, 10000, lambda path: str(path)):
            review_jobs.append(("panoramica", batch))
        for batch in bounded_groups(review_assets["detail"], 4, 10000, lambda path: str(path)):
            review_jobs.append(("dettaglio", batch))

        async def review_layout(index, item):
            kind, batch = item
            if kind == "panoramica":
                instruction = ("Sei il revisore dell'impaginazione. Ogni immagine è una tavola numerata che copre tutte "
                    "le pagine indicate. Controlla struttura, pagine vuote, blocchi tagliati, sovrapposizioni, figure "
                    "fuori margine e discontinuità evidenti. Il testo minuto della tavola non va revisionato parola per parola.")
            else:
                instruction = ("Sei il revisore dell'impaginazione. Controlla in dettaglio queste pagine selezionate "
                    "localmente perché contengono figure, testo piccolo o confini strutturali: leggibilità, formule "
                    "tagliate, glifi mancanti, sovrapposizioni e didascalie separate.")
            system = (prompts.COMMON + "\n" + instruction +
                " Valuta solo difetti visivi osservabili; non certificare la correttezza scientifica. "
                "passed=true se non vi sono difetti major/blocker.")
            review = await self.models.json("gemini", f"Impaginazione ottimizzata {kind} {index+1}", system,
                compact_json({"kind": kind, "files": [path.stem for path in batch],
                              "local_checks": {"pages_checked": layout_scan["pages_checked"],
                                               "minimum_font_pt": layout_scan["minimum_font_pt"]}}),
                Review, images=batch)
            return {"kind": kind, "files": [path.name for path in batch], "review": review}

        self.progress("Controllo visivo gerarchico del PDF", 0.9)
        reviewed = await self.parallel_map(review_jobs, self.settings.gemini_concurrency, review_layout,
            lambda done, total: self.progress(f"Controllo visivo · {done}/{total}", 0.9 + 0.07*done/total))
        layout_reviews = []
        for item in reviewed:
            layout_review = item["review"]
            layout_reviews.append({"kind": item["kind"], "files": item["files"], **layout_review.model_dump()})
            if not layout_review.accepted:
                report["issues"].append("Impaginazione " + ", ".join(item["files"]) + ": revisione visiva da completare.")
                report["issues"].extend(issue.message for issue in layout_review.issues)
        report["layout_reviews"] = layout_reviews
        # If late layout checks add flags, make them visible in the delivered PDF. The body
        # content remains the reviewed version; new front matter is not falsely marked reviewed.
        report["issues"] = list(dict.fromkeys(report["issues"]))
        report["layout_review_scope"] = ("Tutte le pagine controllate localmente e mostrate in panoramiche numerate; "
                                         f"{len(layout_scan['detailed_pages'])} pagine a rischio controllate anche a piena risoluzione.")
        if report["issues"] != printed_issues:
            report["reviewed_layout"] = diagnostics
            report["layout"] = await asyncio.to_thread(build_book, output, self.options.title, [p.model_dump() for p in plans],
                lessons, assets, refs, documents, report, self.options.mode, self.options.output_profile)
            report["final_front_matter_updated"] = True
            report["layout_review_scope"] += " L'elenco iniziale dei rilievi è stato aggiornato dopo la revisione; la versione finale è ricompilata e controllata dal compilatore, senza un secondo giro visivo."
            self.add_layout_issues(report, report["layout"])
            report["issues"] = list(dict.fromkeys(report["issues"]))
        # Re-scan the exact deliverable, including any enlarged front matter. This
        # is local and cheap; it avoids reporting geometry from the pre-update PDF.
        final_scan = await asyncio.to_thread(inspect_pdf_layout, output / "dispensa.pdf")
        report["final_local_layout_scan"] = final_scan
        unseen_local_issues = [issue for issue in final_scan["issues"] if issue not in report["issues"]]
        if unseen_local_issues:
            report["issues"].extend(unseen_local_issues)
            report["issues"] = list(dict.fromkeys(report["issues"]))
            report["layout"] = await asyncio.to_thread(build_book, output, self.options.title,
                [p.model_dump() for p in plans], lessons, assets, refs, documents, report,
                self.options.mode, self.options.output_profile)
            self.add_layout_issues(report, report["layout"])
            report["issues"] = list(dict.fromkeys(report["issues"]))
            report["final_local_layout_scan"] = await asyncio.to_thread(
                inspect_pdf_layout, output / "dispensa.pdf")
        await self.finish(output, report, plans, lessons)

    async def chapter(self, index, plan, folder, topics, visuals, visual_ids, page_map, assets, refs,
                      documents, phase_times=None, editorial_budget=None):
        phase_times = phase_times if phase_times is not None else Counter()
        editorial_budget = editorial_budget or {
            "profile": "transcript", "label": "Compatibilità", "intent": "Versione completa",
            "target_words": 30000, "hard_max_words": 60000, "max_sections": 30,
            "max_paragraphs": 200, "max_exercises": 20, "max_recall": 20,
            "max_concept_maps": 3, "max_authored_charts": 6,
            "allow_generated_exercises": True, "require_concept_map": True,
        }
        # Common, potentially cacheable content stays at the beginning. Both Gemini
        # implicit caching and DeepSeek disk caching match repeated request prefixes.
        relevant_clusters = [{"canonical_title": unit["title"],
                              "topic_ids": [topic_id for topic_id in unit["topic_ids"]
                                            if topic_id in plan.topic_ids]}
                             for unit in getattr(self, "topic_clusters", [])
                             if any(topic_id in plan.topic_ids for topic_id in unit["topic_ids"])]
        context = {"exam_brief": self.options.exam_brief,
                   "editorial_budget": editorial_budget,
                   "topic_clusters": relevant_clusters,
                   "course_guide": self.course_guide.model_dump(),
                   "course_outline": [{"title": chapter["title"], "current": position == index}
                                       for position, chapter in enumerate(self.course_outline)],
                   "source_visual_catalog": source_visual_catalog(
                       getattr(self, "detected_visuals", visuals)),
                   "plan": plan.model_dump(),
                   "topics": {t: topics[t] for t in plan.topic_ids},
                   "source_page_context": source_page_context(topics, plan.topic_ids),
                   "visuals": {v: visuals[v] for v in visual_ids}}
        reviewed_page_ids = sorted({topics[t]["page_id"] for t in plan.topic_ids})
        # For born-digital pages, raw extracted text is cheaper and sharper than
        # resending a full-page raster. Scans stay attached; figure crops stay attached.
        raw_source_text = {page_id: getattr(page_map[page_id], "text", "") for page_id in reviewed_page_ids}
        visual_page_ids = {visuals[visual_id]["page_id"] for visual_id in visual_ids}
        source_images = [self.directory / page_map[page_id].image for page_id in reviewed_page_ids
                         if (len(getattr(page_map[page_id], "text", "").strip()) < 160
                             or page_id in visual_page_ids)]
        feedback = None
        last_lesson = None
        initial_tier = chapter_model_tier(plan, topics)
        for attempt in range(self.options.review_rounds + 1):
            self.check()
            draft_file = folder / f"draft-{attempt}.json"
            lesson = None
            if draft_file.exists():
                try:
                    lesson = Lesson.model_validate(read_json(draft_file))
                    validate_chapter_lesson(lesson, plan.topic_ids, visual_ids, visuals, editorial_budget)
                    self.store.event(self.job_id,
                        f"Capitolo {index+1}: riprendo la bozza locale già generata senza una nuova chiamata.")
                except (ValueError, KeyError):
                    lesson = None
            if lesson is None:
                if feedback and last_lesson is not None:
                    payload = {**context,
                               "current_lesson": last_lesson.model_dump(mode="json"),
                               "review": feedback,
                               "base_sha256": lesson_sha256(last_lesson)}
                    phase_started = time.perf_counter()
                    try:
                        revision = await self.models.json("deepseek",
                            f"Capitolo {index+1}, revisione incrementale {attempt}", prompts.REVISE,
                            compact_json(payload), LessonPatch,
                            validate=lambda patch, base=last_lesson: validate_chapter_lesson(
                                apply_lesson_patch(base, patch), plan.topic_ids, visual_ids, visuals,
                                editorial_budget),
                            max_output=min(12000, max(5000, editorial_budget["hard_max_words"] * 2)),
                            tier="quality")
                    finally:
                        phase_times["deepseek_seconds"] += time.perf_counter() - phase_started
                    # Simple test/replay adapters from StudyGenius 1.1 may return a
                    # complete Lesson directly; live validated providers return LessonPatch.
                    lesson = (revision if isinstance(revision, Lesson)
                              else apply_lesson_patch(last_lesson, revision))
                    validate_chapter_lesson(lesson, plan.topic_ids, visual_ids, visuals, editorial_budget)
                    if isinstance(revision, LessonPatch):
                        atomic_json(folder / f"patch-{attempt}.json", revision.model_dump(mode="json"))
                else:
                    tier = "quality" if initial_tier == "quality" else "fast"
                    phase_started = time.perf_counter()
                    try:
                        lesson = await self.models.json("deepseek", f"Capitolo {index+1}, stesura iniziale", prompts.WRITE,
                            compact_json(context), Lesson,
                            validate=lambda r: validate_chapter_lesson(
                                r, plan.topic_ids, visual_ids, visuals, editorial_budget),
                            max_output=min(32000, max(6000, editorial_budget["hard_max_words"] * 2)),
                            tier=tier)
                    finally:
                        phase_times["deepseek_seconds"] += time.perf_counter() - phase_started
            last_lesson = lesson
            atomic_json(draft_file, lesson.model_dump())
            self.store.update(self.job_id, stage=f"Capitolo {index+1} · verifica formule e grafici ({attempt+1})")
            phase_started = time.perf_counter()
            try:
                lesson, preview_diagnostics = await self.compile_preview_with_repair(
                    index, attempt, plan, lesson, folder, topics, visuals, visual_ids, assets, refs,
                    documents, editorial_budget)
            except LatexError as exc:
                if attempt == self.options.review_rounds:
                    raise
                feedback = {"latex_error": str(exc)[-2500:], "instruction": "Correggi la sintassi matematica segnalata senza omettere contenuti."}
                self.store.event(self.job_id, f"Capitolo {index+1}: correggo un errore di compilazione LaTeX.")
                continue
            finally:
                phase_times["latex_seconds"] += time.perf_counter() - phase_started
            last_lesson = lesson
            atomic_json(folder / f"draft-{attempt}.json", lesson.model_dump())
            phase_started = time.perf_counter()
            try:
                chart_images, map_images = await asyncio.gather(
                    asyncio.to_thread(render_charts, lesson, folder / "plots", f"C{index+1:03d}"),
                    asyncio.to_thread(render_concept_maps, lesson, folder / "maps", f"C{index+1:03d}"))
            finally:
                phase_times["visual_render_seconds"] += time.perf_counter() - phase_started
            review_context = {"exam_brief": self.options.exam_brief,
                              "editorial_budget": editorial_budget,
                              "topic_clusters": context["topic_clusters"],
                              "course_guide": self.course_guide.model_dump(),
                              "source_visual_catalog": context["source_visual_catalog"],
                              "plan": plan.model_dump(),
                              "topics": context["topics"], "source_page_context": context["source_page_context"],
                              "visuals": context["visuals"], "raw_source_text": raw_source_text,
                              "lesson": lesson.model_dump(), "latex_diagnostics": preview_diagnostics,
                              "reviewed_page_ids": reviewed_page_ids}
            review_images = [*source_images, *[Path(assets[v]["review_path"]) for v in visual_ids],
                             *chart_images, *map_images]
            review_file = folder / f"review-{attempt}.json"
            saved_review = None
            if review_file.exists():
                try:
                    checkpoint = read_json(review_file)
                    # A review is reusable only for the exact lesson it judged.
                    # Older unbound Review files are ignored unless final.json
                    # already made the whole chapter independently resumable.
                    if checkpoint.get("lesson_sha256") == lesson_sha256(lesson):
                        saved_review = Review.model_validate(checkpoint["review"])
                except (ValueError, KeyError, AttributeError):
                    saved_review = None
            image_groups = list(bounded_groups(review_images, 12, 16000, lambda path: str(path))) or [[]]

            async def review_part(part_index, image_group):
                return await self.models.json("gemini", f"Revisione capitolo {index+1}.{attempt+1}.{part_index+1}",
                    prompts.REVIEW, compact_json(review_context), Review,
                    images=image_group, max_output=16000)

            if saved_review:
                review = saved_review
                self.store.event(self.job_id,
                    f"Capitolo {index+1}: riuso la revisione locale completata.")
            else:
                phase_started = time.perf_counter()
                try:
                    partial_reviews = await self.parallel_map(image_groups, self.settings.gemini_concurrency, review_part)
                finally:
                    phase_times["gemini_seconds"] += time.perf_counter() - phase_started
                review = Review(passed=all(r.accepted for r in partial_reviews),
                    coverage=min(r.coverage for r in partial_reviews), correctness=min(r.correctness for r in partial_reviews),
                    clarity=min(r.clarity for r in partial_reviews), issues=[i for r in partial_reviews for i in r.issues])
                atomic_json(review_file, {"lesson_sha256": lesson_sha256(lesson),
                                          "review": review.model_dump()})
            if review.accepted or attempt == self.options.review_rounds:
                return lesson, review
            feedback = review.model_dump()
            self.store.event(self.job_id, f"Capitolo {index+1}: DeepSeek corregge i rilievi di Gemini ({attempt+1}/{self.options.review_rounds}).")
        raise RuntimeError("Nessuna versione del capitolo completata")

    async def compile_preview_with_repair(self, index, attempt, plan, lesson, folder, topics,
                                          visuals, visual_ids, assets, refs, documents,
                                          editorial_budget):
        """Compile first; on failure ask the fast text model for narrow field replacements."""
        current = lesson
        last_error = None
        for repair_round in range(3):
            try:
                diagnostics = await asyncio.to_thread(build_book, folder / "preview", current.title,
                    [plan.model_dump()], [current], assets, refs, documents,
                    {"issues": ["Anteprima di lavoro"]}, self.options.mode,
                    self.options.output_profile)
                return current, diagnostics
            except LatexError as exc:
                last_error = exc
                if repair_round == 2:
                    break
                digest = lesson_sha256(current)
                payload = {"base_sha256": digest, "compiler_diagnostic": str(exc)[-3500:],
                           "lesson": current.model_dump(mode="json")}
                repair = await self.models.json("deepseek",
                    f"Capitolo {index+1}, riparazione LaTeX {attempt+1}.{repair_round+1}",
                    prompts.LATEX_REPAIR, compact_json(payload), LessonRepair,
                    validate=lambda value, base=current: apply_lesson_repair(base, value),
                    max_output=6000, tier="fast")
                current = apply_lesson_repair(current, repair)
                validate_chapter_lesson(current, plan.topic_ids, visual_ids, visuals,
                                        editorial_budget)
                atomic_json(folder / f"draft-{attempt}-latex-{repair_round+1}.json", current.model_dump())
                self.store.event(self.job_id,
                    f"Capitolo {index+1}: applicata una correzione LaTeX testuale; ricompilo localmente.")
        raise last_error

    def report(self, pages, evidence, topics, visuals, plans, lessons, reviews, audits):
        issues = []
        for page in evidence:
            issues.extend(f"{page.page_id}: {u}" for u in page.uncertainties)
            issues.extend(f"{page.page_id}, figura {v.title}: {v.uncertainty}" for v in page.visuals if v.uncertainty)
        for i, (lesson, review) in enumerate(zip(lessons, reviews), 1):
            if not review.accepted:
                issues.append(f"Capitolo {i}: revisione scientifica non superata dopo i tentativi previsti.")
            issues.extend(f"Capitolo {i}: {u}" for u in lesson.uncertainties)
            issues.extend(f"Capitolo {i}: {x.message}" for x in review.issues if x.severity in ("major", "blocker"))
        for audit in audits:
            issues.extend(i["message"] for i in audit.get("issues", []))
            if not audit.get("passed") or min(audit.get(k, 0) for k in ("coverage", "correctness", "clarity")) < 90:
                issues.append("La revisione globale del programma non è stata superata.")
        if not self.options.exam_brief.strip():
            issues.append("Programma d'esame non fornito: copertura verificata rispetto ai PDF, non rispetto all'esame.")
        return {"mode": self.options.mode, "issues": list(dict.fromkeys(issues)),
                "coverage": {"pages_total": len(pages), "pages_analyzed": len(evidence),
                             "topics_total": len(topics), "topics_in_lessons": len({t for l in lessons for s in l.sections for t in s.topic_ids}),
                             "visuals_total": len(visuals), "visuals_explained": len({v.visual_id for l in lessons for v in l.visuals})},
                "excluded_pages": [{"page_id": p.page_id, "reason": p.excluded_reason} for p in evidence if p.excluded_reason],
                "topic_map": {t: {"page_id": v["page_id"], "title": v["title"],
                                  "chapter": next(i+1 for i,p in enumerate(plans) if t in p.topic_ids)} for t,v in topics.items()},
                "chapter_reviews": [r.model_dump() for r in reviews], "curriculum_reviews": audits,
                "scope": "Copertura degli argomenti identificati dai modelli; non prova di assenza di omissioni semantiche."}

    @staticmethod
    def add_layout_issues(report, diagnostics):
        if diagnostics["overfull_boxes"]:
            report["issues"].append("Il compilatore segnala elementi oltre i margini: controllare l'impaginazione.")
        if diagnostics["missing_characters"]:
            report["issues"].append("Il compilatore segnala glifi mancanti: controllare le formule e i simboli.")
        if diagnostics["empty_pages"]:
            report["issues"].append("Il PDF contiene pagine quasi vuote: verificare il rapporto di impaginazione.")

    async def finish(self, output, report, plans, lessons):
        self.check()
        report["usage"] = self.store.usage(self.job_id)
        report["cache"] = self.models.cache_stats if self.models else {}
        report["options"] = self.options.model_dump()
        report["models"] = ({"reader_reviewer": self.settings.gemini_model,
                             "planner_fast": self.settings.deepseek_fast_model,
                             "author_reasoning": self.settings.deepseek_model,
                             "author_reasoning_effort": self.settings.deepseek_reasoning_effort}
                            if self.options.mode == "live" else {"reader_reviewer": None, "planner_author": None})
        atomic_json(output / "qualita.json", report)
        atomic_json(output / "contenuti.json", {"plans": [p.model_dump() for p in plans], "lessons": [l.model_dump() for l in lessons]})
        text = "# Rapporto StudyGenius\n\n" + report.get("scope", "Dimostrazione offline; nessuna verifica API live.") + "\n\n"
        text += "## Controlli\n\n" + "\n".join(f"- {k}: {v}" for k,v in report.get("coverage", {}).items()) + "\n\n"
        text += "## Punti da verificare\n\n" + ("\n".join("- " + i for i in report["issues"]) or "Nessun problema residuo segnalato dai controlli automatici.") + "\n"
        (output / "qualita.md").write_text(text, encoding="utf-8")
        await asyncio.to_thread(source_archive, output, [output / "qualita.json", output / "qualita.md", output / "contenuti.json",
            self.directory / "evidence.json", self.directory / "outline.json", self.directory / "course-guide.json", self.directory / "inputs.json"])
        status = "needs_review" if report["issues"] else "completed"
        self.store.update(self.job_id, status=status, stage="PDF pronto · controlla i punti segnalati" if report["issues"] else "PDF pronto",
                          progress=1, error="")
        self.store.event(self.job_id, "PDF, sorgenti LaTeX e rapporto di qualità disponibili.")
