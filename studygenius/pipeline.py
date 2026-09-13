from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path

from . import prompts
from .config import Settings
from .ingest import crop_visual, ingest
from .models import ChapterPlan, CourseGuide, EvidenceBatch, JobOptions, Lesson, Outline, PageAnalysis, Review
from .providers import Models
from .render import (LatexError, build_book, render_charts, render_pdf_pages,
                     source_archive, validate_lesson_math, preflight_latex)
from .storage import Store, atomic_json, read_json


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


def validate_lesson(lesson: Lesson, topic_ids: list[str], visual_ids: list[str]):
    expected = set(topic_ids)
    actual = {t for s in lesson.sections for t in s.topic_ids}
    if actual != expected:
        raise ValueError(f"La lezione deve trattare esattamente questi topic_ids: {topic_ids}")
    for item in [*lesson.exercises, *lesson.charts]:
        if not set(item.topic_ids) <= expected:
            raise ValueError("Esercizio/grafico con riferimenti a fonti inesistenti nel capitolo")
    if Counter(v.visual_id for v in lesson.visuals) != Counter(visual_ids):
        raise ValueError(f"Spiegare esattamente una volta ciascun visual_id: {visual_ids}")
    validate_lesson_math(lesson)


def validate_chapter_lesson(lesson: Lesson, topic_ids, visual_ids, known_visual_ids):
    # Other known figures already have an owner in the course plan. Ignore those
    # extra explanations instead of regenerating an otherwise valid chapter.
    # Missing assigned figures, duplicates and unknown IDs still fail below.
    assigned, known = set(visual_ids), set(known_visual_ids)
    lesson.visuals = [v for v in lesson.visuals if v.visual_id in assigned or v.visual_id not in known]
    validate_lesson(lesson, topic_ids, visual_ids)


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
        page_groups = list(bounded_groups(pages, self.options.pages_per_batch, 140000, lambda p: p.model_dump()))
        self.store.event(self.job_id, f"{len(pages)} pagine, {len(page_groups)} blocchi di lettura visiva. Nessuna pagina viene saltata.")
        for index, batch in enumerate(page_groups):
            self.check()
            checkpoint = self.directory / "evidence" / f"batch-{index:04d}.json"
            expected = [p.id for p in batch]
            if checkpoint.exists():
                result = EvidenceBatch.model_validate(read_json(checkpoint))
                validate_evidence(result, expected)
            else:
                self.progress(f"Gemini legge le pagine · {index+1}/{len(page_groups)}", 0.08 + 0.22*index/len(page_groups))
                result = await self.models.json("gemini", f"Lettura blocco {index+1}", prompts.READ,
                    json.dumps({"pages": [{"page_id": p.id, "filename": p.filename, "page": p.number, "text": p.text} for p in batch]}, ensure_ascii=False),
                    EvidenceBatch, images=[self.directory / p.image for p in batch],
                    validate=lambda r: validate_evidence(r, expected), max_output=24000)
                atomic_json(checkpoint, result.model_dump())
            evidence.extend(result.pages)
        # Restore original page order even when the model returned pages in a different order.
        order = {p.id: i for i, p in enumerate(pages)}
        evidence.sort(key=lambda p: order[p.page_id])
        atomic_json(self.directory / "evidence.json", [p.model_dump() for p in evidence])
        topics, visuals = {}, {}
        for page in evidence:
            for i, topic in enumerate(page.topics, 1):
                topics[f"{page.page_id}-T{i:02d}"] = {"page_id": page.page_id, **topic.model_dump()}
            for i, visual in enumerate(page.visuals, 1):
                visuals[f"{page.page_id}-V{i:02d}"] = {"page_id": page.page_id, **visual.model_dump()}
        if not topics:
            raise ValueError("Nessun contenuto didattico leggibile: controlla i PDF e le pagine escluse in evidence.json.")
        self.progress("Costruzione del percorso di studio", 0.31, f"Individuati {len(topics)} argomenti e {len(visuals)} figure da spiegare.")
        outline_file = self.directory / "outline.json"
        if outline_file.exists():
            plans = [ChapterPlan.model_validate(x) for x in read_json(outline_file)]
        else:
            plans = []
            inventories = [{"id": key, "title": value["title"], "kind": value["kind"]} for key, value in topics.items()]
            for index, batch in enumerate(bounded_groups(inventories, 100, 70000)):
                outline = await self.models.json("deepseek", f"Indice {index+1}", prompts.PLAN,
                    json.dumps({"exam_brief": self.options.exam_brief, "topics": batch}, ensure_ascii=False), Outline,
                    validate=lambda r: validate_outline(r, [t["id"] for t in batch]))
                for plan in outline.chapters:
                    # Bound writing context independently of how the planner groups topics.
                    groups = list(bounded_groups(plan.topic_ids, 10, 60000, lambda t: topics[t]))
                    for n, group in enumerate(groups, 1):
                        title = plan.title + (f" - Parte {n}" if len(groups) > 1 else "")
                        plans.append(ChapterPlan(title=title[:180], topic_ids=group,
                                                 objectives=plan.objectives, prerequisites=plan.prerequisites))
            atomic_json(outline_file, [p.model_dump() for p in plans])
        if Counter(t for p in plans for t in p.topic_ids) != Counter(topics.keys()):
            raise ValueError("Il checkpoint dell'indice non copre gli argomenti estratti")
        self.progress("Allineamento delle convenzioni tra capitoli", 0.33)
        guide_file = self.directory / "course-guide.json"
        if guide_file.exists():
            self.course_guide = CourseGuide.model_validate(read_json(guide_file))
        else:
            guides = []
            for batch in bounded_groups(list(topics.items()), 100, 80000):
                guides.append(await self.models.json("gemini", "Convenzioni del corso", prompts.GUIDE,
                    json.dumps(dict(batch), ensure_ascii=False), CourseGuide, max_output=8000))
            merged = {key: list(dict.fromkeys(item for guide in guides for item in getattr(guide, key)))
                      for key in ("conventions", "symbols", "conflicts")}
            if len(json.dumps(merged)) > 40000 or len(guides) > 1:
                # Merge compact extraction summaries, preserving conflicts explicitly.
                if len(json.dumps(merged)) > 180000:
                    raise ValueError("Le convenzioni del corso superano il contesto: separa i corsi in progetti distinti.")
                self.course_guide = await self.models.json("gemini", "Sintesi delle convenzioni", prompts.GUIDE,
                    json.dumps(merged, ensure_ascii=False), CourseGuide, max_output=10000)
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
        for visual_id, value in visuals.items():
            self.check()
            page = page_map[value["page_id"]]
            target = self.directory / "visuals" / f"{visual_id}.png"
            visual = SourceVisual.model_validate({k: v for k, v in value.items() if k != "page_id"})
            await asyncio.to_thread(crop_visual, self.directory, page, visual, target)
            assets[visual_id] = {"path": str(target), "title": value["title"],
                                 "reference": f"{page.document}, p. {page.number}"}
        documents = read_json(self.directory / "inputs.json")
        lessons, reviews = [], []
        for index, plan in enumerate(plans):
            self.check()
            self.progress(f"Capitolo {index+1}/{len(plans)} · scrittura e revisione", 0.34 + 0.46*index/len(plans), plan.title)
            chapter_dir = self.directory / "chapters" / f"C{index+1:03d}"
            chapter_dir.mkdir(parents=True, exist_ok=True)
            final = chapter_dir / "final.json"
            visual_ids = visual_assignments[index]
            saved = read_json(final) if final.exists() else None
            if saved and saved.get("guide_digest") == guide_digest:
                lesson, review = Lesson.model_validate(saved["lesson"]), Review.model_validate(saved["review"])
                validate_lesson(lesson, plan.topic_ids, visual_ids)
            else:
                lesson, review = await self.chapter(index, plan, chapter_dir, topics, visuals, visual_ids, page_map, assets, refs, documents)
                atomic_json(final, {"lesson": lesson.model_dump(), "review": review.model_dump(), "guide_digest": guide_digest})
            lessons.append(lesson)
            reviews.append(review)
        self.progress("Verifica del programma d'esame", 0.81)
        audit_reviews = []
        # Hierarchical audit: an exhaustive inventory is provided in bounded batches. A final
        # syllabus audit uses all chapter titles/objectives without the lengthy source text.
        catalog = [{"chapter": p.title, "objectives": p.objectives,
                    "topics": [topics[t]["title"] for t in p.topic_ids]} for p in plans]
        catalog_text = json.dumps(catalog, ensure_ascii=False)
        if len(catalog_text) <= 180000:
            audit = await self.models.json("gemini", "Copertura del programma", prompts.AUDIT,
                json.dumps({"exam_brief": self.options.exam_brief, "catalog": catalog}, ensure_ascii=False), Review)
            audit_reviews.append(audit.model_dump())
        else:
            audit_reviews.append({"passed": False, "issues": [{"severity": "major", "message":
                "Indice molto esteso: confronto globale con il programma non eseguito. Verificare la copertura manualmente."}]})
        report = self.report(pages, evidence, topics, visuals, plans, lessons, reviews, audit_reviews)
        report["course_guide"] = self.course_guide.model_dump()
        report["issues"].extend(self.course_guide.conflicts)
        output = self.directory / "output"
        printed_issues = list(report["issues"])
        self.progress("Composizione e compilazione LaTeX", 0.86, "Creo il PDF con testo selezionabile, formule e figure.")
        diagnostics = await asyncio.to_thread(build_book, output, self.options.title, [p.model_dump() for p in plans],
            lessons, assets, refs, documents, report)
        report["layout"] = diagnostics
        self.add_layout_issues(report, diagnostics)
        self.check()
        # Review the rendered pages, not merely the markup. All pages are checked in small batches.
        images = await asyncio.to_thread(render_pdf_pages, output / "dispensa.pdf", self.directory / "layout")
        layout_reviews = []
        for index, batch in enumerate(bounded_groups(images, 4, 10000, lambda p: str(p))):
            self.progress(f"Controllo visivo del PDF · pagine {index*4+1}-{index*4+len(batch)}", 0.9 + 0.07*index/max(1, (len(images)+3)//4))
            layout_review = await self.models.json("gemini", f"Impaginazione {index+1}", prompts.COMMON +
                "\nSei il revisore dell'impaginazione. Controlla queste pagine renderizzate: testo e formule tagliati, "
                "sovrapposizioni, glifi mancanti, figure illeggibili, didascalie separate in modo incomprensibile. "
                "Valuta solo difetti visivi osservabili; non certificare la correttezza scientifica. "
                "passed=true se non vi sono difetti major/blocker. Usa lo schema Review.",
                "Immagini delle pagine finali del documento: " + ", ".join(p.stem for p in batch), Review, images=batch)
            layout_reviews.append(layout_review.model_dump())
            if not layout_review.accepted:
                report["issues"].append(f"Impaginazione pagine {index*4+1}-{index*4+len(batch)}: revisione visiva da completare.")
                report["issues"].extend(i.message for i in layout_review.issues)
        report["layout_reviews"] = layout_reviews
        # If late layout checks add flags, make them visible in the delivered PDF. The body
        # content remains the reviewed version; new front matter is not falsely marked reviewed.
        report["issues"] = list(dict.fromkeys(report["issues"]))
        report["layout_review_scope"] = "Tutte le pagine della versione sottoposta al revisore visivo."
        if report["issues"] != printed_issues:
            report["reviewed_layout"] = diagnostics
            report["layout"] = await asyncio.to_thread(build_book, output, self.options.title, [p.model_dump() for p in plans],
                lessons, assets, refs, documents, report)
            report["final_front_matter_updated"] = True
            report["layout_review_scope"] += " L'elenco iniziale dei rilievi è stato aggiornato dopo la revisione; la versione finale è ricompilata e controllata dal compilatore, senza un secondo giro visivo."
            self.add_layout_issues(report, report["layout"])
            report["issues"] = list(dict.fromkeys(report["issues"]))
        await self.finish(output, report, plans, lessons)

    async def chapter(self, index, plan, folder, topics, visuals, visual_ids, page_map, assets, refs, documents):
        context = {"exam_brief": self.options.exam_brief, "plan": plan.model_dump(),
                   "course_guide": self.course_guide.model_dump(), "course_outline": self.course_outline,
                   "topics": {t: topics[t] for t in plan.topic_ids},
                   "source_page_context": source_page_context(topics, plan.topic_ids),
                   "source_visual_catalog": source_visual_catalog(visuals),
                   "visuals": {v: visuals[v] for v in visual_ids}}
        source_images = [self.directory / page_map[p].image for p in sorted({topics[t]["page_id"] for t in plan.topic_ids})]
        feedback = None
        last_lesson = None
        for attempt in range(self.options.review_rounds + 1):
            self.check()
            content = json.dumps(context, ensure_ascii=False)
            if feedback:
                content += "\nCorreggi questa versione precedente conservando tutto ciò che è corretto:\n"
                content += last_lesson.model_dump_json() + "\nProblemi da risolvere:\n" + json.dumps(feedback, ensure_ascii=False)
            lesson = await self.models.json("deepseek", f"Capitolo {index+1}, stesura {attempt+1}", prompts.WRITE,
                content, Lesson, validate=lambda r: validate_chapter_lesson(r, plan.topic_ids, visual_ids, visuals), max_output=32000)
            last_lesson = lesson
            atomic_json(folder / f"draft-{attempt}.json", lesson.model_dump())
            self.progress(f"Capitolo {index+1} · verifica formule e grafici ({attempt+1})", 0.34 + 0.46*index/max(1, len(read_json(self.directory / "outline.json"))))
            try:
                preview_diagnostics = await asyncio.to_thread(build_book, folder / "preview", lesson.title, [plan.model_dump()],
                    [lesson], assets, refs, documents, {"issues": ["Anteprima di lavoro"]})
            except LatexError as exc:
                if attempt == self.options.review_rounds:
                    raise
                feedback = {"latex_error": str(exc)[-2500:], "instruction": "Correggi la sintassi matematica segnalata senza omettere contenuti."}
                self.store.event(self.job_id, f"Capitolo {index+1}: correggo un errore di compilazione LaTeX.")
                continue
            chart_images = await asyncio.to_thread(render_charts, lesson, folder / "plots", f"C{index+1:03d}")
            review_context = {**context, "lesson": lesson.model_dump(), "latex_diagnostics": preview_diagnostics,
                              "source_visual_catalog": source_visual_catalog(visuals),
                              "reviewed_page_ids": sorted({topics[t]["page_id"] for t in plan.topic_ids})}
            review_images = [*source_images, *[Path(assets[v]["path"]) for v in visual_ids], *chart_images]
            # Each part checks the full textual evidence. Split only image payloads to stay bounded.
            partial_reviews = []
            for part_index, image_group in enumerate(bounded_groups(review_images, 8, 10000, lambda p: str(p))):
                partial_reviews.append(await self.models.json("gemini", f"Revisione capitolo {index+1}.{attempt+1}.{part_index+1}",
                    prompts.REVIEW, json.dumps(review_context, ensure_ascii=False), Review, images=image_group, max_output=16000))
            review = Review(passed=all(r.accepted for r in partial_reviews),
                coverage=min(r.coverage for r in partial_reviews), correctness=min(r.correctness for r in partial_reviews),
                clarity=min(r.clarity for r in partial_reviews), issues=[i for r in partial_reviews for i in r.issues])
            atomic_json(folder / f"review-{attempt}.json", review.model_dump())
            if review.accepted or attempt == self.options.review_rounds:
                return lesson, review
            feedback = review.model_dump()
            # Scientific feedback must not shrink a readable table into a whole page.
            bad_crop = any(i.severity in ("major", "blocker") and
                           any(word in (i.target + " " + i.message + " " + i.correction).lower()
                               for word in ("ritagl", "crop")) for i in review.issues)
            for visual_id in visual_ids if bad_crop else []:
                from .models import SourceVisual
                value = visuals[visual_id]
                full = SourceVisual(title=value["title"], description=value["description"], bbox=[0, 0, 1000, 1000])
                await asyncio.to_thread(crop_visual, self.directory, page_map[value["page_id"]], full, Path(assets[visual_id]["path"]))
            self.store.event(self.job_id, f"Capitolo {index+1}: DeepSeek corregge i rilievi di Gemini ({attempt+1}/{self.options.review_rounds}).")
        raise RuntimeError("Nessuna versione del capitolo completata")

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
        report["options"] = self.options.model_dump()
        report["models"] = ({"reader_reviewer": self.settings.gemini_model, "planner_author": self.settings.deepseek_model}
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
