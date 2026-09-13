"""Trusted LaTeX composition. Only whitelisted math reaches TeX; plots use numeric data."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
import zipfile
from pathlib import Path

import fitz
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .models import Lesson

MATH_COMMANDS = set(r"""frac dfrac tfrac sqrt overline underline underbrace overbrace
vec hat bar dot ddot tilde widehat widetilde boldsymbol mathbf mathrm mathit mathcal mathbb
text textrm textbf operatorname left right middle big Big bigg Bigg bigl bigr Bigl Bigr
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu
xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
int iint iiint oint sum prod lim min max sup inf sin cos tan cot sec csc sinh cosh tanh
arcsin arccos arctan exp log ln det gcd mod bmod pmod
infty partial nabla cdot times div pm mp le leq ge geq neq ne approx sim simeq equiv
propto to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto
longrightarrow longleftrightarrow implies iff in notin subset subseteq supset supseteq
cup cap emptyset forall exists neg land lor lnot lvert rvert vert lVert rVert Vert
langle rangle lbrace rbrace ldots cdots vdots ddots dots quad qquad hspace phantom
displaystyle textstyle scriptstyle scriptscriptstyle binom dbinom tbinom overset underset
substack limits nolimits degree circ angle perp parallel ell hbar Re Im Pr
begin end boxed cancel ce SI si mbox mathsf mathscr numberwithin
qquad colon mid nonumber notag
""".split()) - {"hspace", "phantom", "ce", "SI", "si", "mathscr", "numberwithin", "cancel"}
MATH_ENVS = {"aligned", "gathered", "cases", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "smallmatrix"}
UNICODE_MATH = {"α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "δ": r"\delta ",
    "Δ": r"\Delta ", "ε": r"\varepsilon ", "θ": r"\theta ", "λ": r"\lambda ",
    "μ": r"\mu ", "π": r"\pi ", "ρ": r"\rho ", "σ": r"\sigma ", "τ": r"\tau ",
    "φ": r"\phi ", "ω": r"\omega ", "Ω": r"\Omega ", "∞": r"\infty ",
    "∂": r"\partial ", "∇": r"\nabla ", "∫": r"\int ", "≤": r"\leq ",
    "≥": r"\geq ", "≠": r"\neq ", "≈": r"\approx ", "×": r"\times ",
    "·": r"\cdot ", "−": "-", "→": r"\to ", "°": r"^{\circ}"}


def safe_math(value: str) -> str:
    value = value.strip()
    # Some JSON-mode models escape TeX twice. Decode a whole extra escape layer
    # only when known commands are doubled and no normal commands occur. Correct
    # aligned/matrix row separators remain intact (four backslashes become two).
    doubled = re.findall(r"(?<!\\)\\\\([A-Za-z]+)", value)
    normal = re.search(r"(?<!\\)\\[A-Za-z]+", value)
    if any(command in MATH_COMMANDS for command in doubled) and not normal:
        value = value.replace("\\\\", "\\")
    if len(value) > 4000 or any(c in value for c in ("^^", "$", "\x00")):
        raise ValueError("Formula troppo lunga o con delimitatori non ammessi")
    value = "".join(UNICODE_MATH.get(c, c) for c in value)
    stack = []
    for command in re.finditer(r"\\([A-Za-z]+|.)", value):
        name = command.group(1)
        if name not in MATH_COMMANDS and name not in {"\\", ",", ";", ":", "!", " ", "{", "}", "%", "_", "#", "&", "|"}:
            raise ValueError(f"Comando matematico non ammesso: {name}")
    for m in re.finditer(r"\\(begin|end)\{([^{}]+)\}", value):
        action, env = m.groups()
        if env not in MATH_ENVS:
            raise ValueError(f"Ambiente matematico non ammesso: {env}")
        if action == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            raise ValueError("Ambienti matematici non bilanciati")
    if stack:
        raise ValueError("Ambiente matematico non chiuso")
    if "\\\\" in value and not re.search(r"\\begin\{(?:aligned|gathered|cases|[pbvV]?matrix|smallmatrix)\}", value):
        raise ValueError("Interruzione di riga matematica fuori da aligned/matrix; non raddoppiare gli escape JSON delle formule")
    # Strip escaped characters before checking TeX group balance and parameter/comment chars.
    stripped = re.sub(r"\\[^A-Za-z]", "", value)
    if "%" in stripped or "#" in stripped:
        raise ValueError("Usare percentuale e cancelletto con escape nelle formule")
    level = 0
    for char in stripped:
        if char == "{":
            level += 1
        if char == "}":
            level -= 1
        if level < 0 or level > 40:
            raise ValueError("Parentesi graffe matematiche non valide")
    if level:
        raise ValueError("Parentesi graffe matematiche non bilanciate")
    return value


def escape(value: str) -> str:
    chars = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
             "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(chars.get(c, c) for c in value if c in "\n\t" or ord(c) >= 32)


def rich(value: str) -> str:
    # Accept $...$, \(...\), and \[...\] as content; never interpret surrounding TeX.
    pattern = r"(?<!\\)\$([^$\n]+?)(?<!\\)\$|\\\((.*?)\\\)|\\\[(.*?)\\\]"
    result, last = [], 0
    for match in re.finditer(pattern, value, re.DOTALL):
        result.append(prose(value[last:match.start()]))
        math = next(x for x in match.groups() if x is not None)
        result.append(r"\(" + safe_math(math) + r"\)")
        last = match.end()
    result.append(prose(value[last:]))
    return "".join(result)


def prose(value: str) -> str:
    """Typeset unmistakable bare scientific notation without treating prose as TeX.

    Handles model output such as 'Delta U', V_i and m^3 outside $...$. Only
    whitelisted TeX commands/short subscripted or exponentiated symbols qualify.
    """
    start = re.compile(r"\\{1,2}[A-Za-z]+|\b[A-Za-z]{1,3}(?=[_^])|\b\d{1,3}(?=\^)")
    result, last, position = [], 0, 0
    while match := start.search(value, position):
        token = match.group()
        if token.startswith("\\") and token.lstrip("\\") not in MATH_COMMANDS:
            position = match.end()
            continue
        end = match.end()
        # Consume balanced arguments and indices, including nested \text{...}.
        while end < len(value):
            if value[end] in "_^":
                end += 1
                if end < len(value) and value[end] != "{":
                    suffix = re.match(r"-?\d+|[A-Za-z0-9]+", value[end:])
                    if suffix:
                        end += len(suffix.group())
                        continue
            if end < len(value) and value[end] == "{":
                depth, cursor = 1, end + 1
                while cursor < len(value) and depth:
                    if value[cursor] == "{": depth += 1
                    if value[cursor] == "}": depth -= 1
                    cursor += 1
                if depth:
                    break
                end = cursor
                continue
            break
        # Attach a following single-letter variable to a Greek/operator command.
        if token.startswith("\\"):
            tail = re.match(r" [A-Za-z](?![A-Za-z])", value[end:])
            if tail:
                end += len(tail.group())
        candidate = value[match.start():end]
        # Bare alphabetic multi-letter indices (p_ext) denote a label, not e*x*t.
        candidate = re.sub(r"_([A-Za-z]{2,})(?![A-Za-z])", r"_{\1}", candidate)
        candidate = re.sub(r"\^(-\d+)", r"^{\1}", candidate)
        try:
            math = safe_math(candidate)
        except ValueError:
            position = max(end, match.end())
            continue
        result.extend([escape(value[last:match.start()]), r"\(" + math + r"\)"])
        last = end
        position = end
    result.append(escape(value[last:]))
    return "".join(result)


def validate_lesson_math(lesson: Lesson):
    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            safe_math(value) if key in ("latex", "math") else rich(value)
    walk(lesson.model_dump())


def display(value: str) -> str:
    return (r"\begin{center}\begin{adjustbox}{max width=\linewidth}$\displaystyle "
            + safe_math(value) + r"$\end{adjustbox}\end{center}" + "\n")


def heading(value: str) -> str:
    # Typeset formula delimiters in headings and the TOC; keep bookmark text plain.
    bookmark = re.sub(r"[\$\\{}]", "", value)
    return r"\texorpdfstring{" + rich(value) + "}{" + escape(bookmark) + "}"


def bullets(items: list[str], numbered=False) -> str:
    if not items:
        return ""
    env = "enumerate" if numbered else "itemize"
    if numbered:
        items = [re.sub(r"^\s*\d{1,3}(?:\)\s*|\.\s+)", "", item) for item in items]
    return f"\\begin{{{env}}}\n" + "\n".join(r"\item " + rich(s) for s in items) + f"\n\\end{{{env}}}\n"


def render_charts(lesson: Lesson, assets: Path, prefix: str) -> list[Path]:
    assets.mkdir(exist_ok=True, parents=True)
    paths = []
    with plt.rc_context({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                         "text.parse_math": False, "axes.prop_cycle": plt.cycler(color=["#177e89", "#ba5b3b", "#7354a0", "#496d43", "#b2902e", "#264d77"])}):
        for i, chart in enumerate(lesson.charts):
            fig, ax = plt.subplots(figsize=(7.6, 4.6), layout="constrained")
            try:
                for series in chart.series:
                    if chart.kind == "line":
                        ax.plot(series.x, series.y, label=textwrap.fill(series.label, 34), linewidth=2)
                    else:
                        ax.scatter(series.x, series.y, label=textwrap.fill(series.label, 34), s=28)
                ax.set(xlabel=textwrap.fill(chart.xlabel, 75), ylabel=textwrap.fill(chart.ylabel, 55))
                ax.set_title(textwrap.fill(chart.title, 70), loc="left", pad=16, weight="bold")
                ax.grid(alpha=0.18)
                ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=min(2, len(chart.series)), frameon=False)
                base = assets / f"{prefix}-chart-{i+1:02d}"
                for ext in ("svg", "pdf", "png"):
                    fig.savefig(base.with_suffix("." + ext), dpi=160, bbox_inches="tight")
                paths.append(base.with_suffix(".png"))
            finally:
                plt.close(fig)
    return paths


def latex_engine() -> str | None:
    return next((shutil.which(name) for name in ("xelatex", "lualatex") if shutil.which(name)), None)


class LatexError(RuntimeError):
    pass


def compile_tex(folder: Path) -> dict:
    engine = latex_engine()
    if not engine:
        raise LatexError("Manca LaTeX: installa MiKTeX (Windows) o TeX Live con XeLaTeX. Vedi README.")
    target = folder / "dispensa.pdf"
    target.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update({"openin_any": "p", "openout_any": "p", "shell_escape": "f"})
    # No API keys are passed to the compiler process.
    for name in list(env):
        if any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            env.pop(name)
    for _ in range(2):
        try:
            done = subprocess.run([engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
                                   "-file-line-error", "dispensa.tex"], cwd=folder, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        except subprocess.TimeoutExpired:
            target.unlink(missing_ok=True)
            raise LatexError("Compilazione LaTeX oltre 180 secondi: verifica installazione e pacchetti.") from None
        if done.returncode:
            target.unlink(missing_ok=True)
            log = done.stdout.decode("utf-8", errors="replace")
            lines = log.splitlines()
            errors = [n for n, line in enumerate(lines) if "dispensa.tex:" in line or line.startswith("!")]
            start = errors[-1] if errors else max(0, len(lines) - 18)
            detail = "\n".join(lines[max(0, start-1):start+9])
            raise LatexError("Compilazione LaTeX fallita: " + detail)
    if not target.is_file():
        raise LatexError("LaTeX non ha prodotto un PDF")
    log = (folder / "dispensa.log").read_text(errors="replace")
    overflow = [float(x) for x in re.findall(r"Overfull \\[hv]box \(([\d.]+)pt too (?:wide|high)\)", log)]
    missing_chars = list(dict.fromkeys(re.findall(r"Missing character: (.+)", log)))
    return {"engine": Path(engine).name, "overfull_boxes": [v for v in overflow if v > 2], "missing_characters": missing_chars}


def preflight_latex(folder: Path) -> None:
    """Fail before spending API credit if required TeX packages/fonts cannot compile."""
    folder.mkdir(parents=True, exist_ok=True)
    template = (Path(__file__).parent / "templates" / "book.tex").read_text(encoding="utf-8")
    content = (r"\chapter*{Verifica locale}Testo italiano: perché, quantità, energia. "
               r"\begin{tcolorbox}Formula di controllo: \(pV=nRT\).\end{tcolorbox}")
    (folder / "dispensa.tex").write_text(template.replace("%%CONTENT%%", content), encoding="utf-8")
    diagnostics = compile_tex(folder)
    if diagnostics["missing_characters"]:
        raise LatexError("L'installazione LaTeX non dispone dei glifi necessari: verifica i font.")


def build_book(folder: Path, title: str, plans: list[dict], lessons: list[Lesson], visual_assets: dict,
               topic_refs: dict, documents: list[dict], report: dict, mode="live") -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    template = (Path(__file__).parent / "templates" / "book.tex").read_text(encoding="utf-8")
    flags = report.get("issues", [])
    status = "DIMOSTRAZIONE OFFLINE" if mode == "demo" else ("DA VERIFICARE" if flags else "REVISIONI AUTOMATICHE COMPLETATE")
    parts = [r"\begin{titlepage}\sffamily", r"{\color{accent}\Large STUDYGENIUS}\par",
             r"\vspace{20mm}{\Huge\bfseries " + escape(title) + r"}\par",
             r"\vspace{8mm}{\Large Dispensa ragionata per lo studio}\par",
             r"\vspace{16mm}\begin{tcolorbox}[colback=light,colframe=accent,title=" + escape(status) + "]",
             "Teoria, passaggi matematici, figure commentate ed esercizi svolti.",
             r"\end{tcolorbox}\vfill",
             r"\textbf{Metodo di lettura}\par Studia il capitolo, risolvi gli esercizi prima di leggere lo svolgimento e rispondi alle domande di richiamo senza consultare le soluzioni.\par\medskip",
             "Le revisioni automatiche aiutano a individuare errori e omissioni, ma non certificano la correttezza scientifica né il superamento dell'esame. Confronta il programma ufficiale e i punti segnalati con il docente.",
             r"\end{titlepage}\tableofcontents\clearpage",
             r"\chapter*{Prima di iniziare}\addcontentsline{toc}{chapter}{Prima di iniziare}"]
    if mode == "demo":
        parts.append("Questo documento usa contenuti dimostrativi prestabiliti. Non è stato scritto o revisionato da chiamate API live. I consumi della dimostrazione sono zero.")
    parts.append("I riferimenti D001, D002, ecc. identificano i documenti elencati in appendice. I numeri di pagina indicano le pagine fisiche del PDF caricato, a partire da 1.")
    if flags:
        parts.append(r"\begin{tcolorbox}[breakable,colback=warm,colframe=rust,title=Punti da verificare]")
        parts.append(bullets([str(i) for i in flags]))
        parts.append(r"\end{tcolorbox}")
    for index, (plan, lesson) in enumerate(zip(plans, lessons), 1):
        prefix = f"C{index:03d}"
        parts.extend([r"\chapter{" + heading(lesson.title) + "}", rich(lesson.introduction),
                      r"\section*{Obiettivi}", bullets(plan["objectives"])])
        if plan.get("prerequisites"):
            parts.extend([r"\textbf{Prerequisiti}", bullets(plan["prerequisites"])])
        for section in lesson.sections:
            refs = sorted({topic_refs[t] for t in section.topic_ids})
            parts.extend([r"\Needspace{8\baselineskip}\section{" + heading(section.title) + "}",
                          r"{\small\color{muted}Fonti: " + escape("; ".join(refs)) + r"}\par\medskip"])
            for p in section.paragraphs:
                parts.append(rich(p) + "\n\n")
            for equation in section.equations:
                parts.extend([r"\Needspace{12\baselineskip}\begin{tcolorbox}[breakable,colback=light,colframe=accent]", display(equation.latex),
                              rich(equation.explanation), bullets(equation.symbols),
                              r"\textbf{Condizioni di validità.} " + rich(equation.assumptions), r"\end{tcolorbox}"])
            for step in section.derivation:
                parts.extend([r"\subsubsection*{" + heading(step.label) + "}", rich(step.explanation)])
                if step.math:
                    parts.append(display(step.math))
            if section.pitfalls:
                parts.extend([r"\paragraph{Errori da evitare}", bullets(section.pitfalls)])
        for visual in lesson.visuals:
            asset = visual_assets[visual.visual_id]
            name = Path(asset["path"]).name
            target = folder / "assets" / name
            target.parent.mkdir(exist_ok=True)
            if Path(asset["path"]).resolve() != target.resolve():
                shutil.copy2(asset["path"], target)
            parts.extend([r"\Needspace{0.6\textheight}\section*{Leggere la figura: " + heading(asset["title"]) + "}",
                          r"\begin{center}\includegraphics[width=\linewidth,height=0.48\textheight,keepaspectratio]{assets/" + name + r"}\end{center}",
                          r"{\small Fonte originale: " + escape(asset["reference"]) + r"}\par",
                          bullets(visual.how_to_read, True), rich(visual.meaning), bullets(visual.takeaways),
                          r"\paragraph{Limiti di lettura} " + rich(visual.limitations)])
        render_charts(lesson, folder / "assets", prefix)
        for i, chart in enumerate(lesson.charts, 1):
            parts.extend([r"\Needspace{0.52\textheight}\section*{Grafico ricostruito: " + heading(chart.title) + "}",
                          r"\begin{center}\includegraphics[width=\linewidth]{assets/" + f"{prefix}-chart-{i:02d}.pdf" + r"}\end{center}",
                          r"\textbf{Provenienza dei dati.} " + rich(chart.provenance), "\n\n" + rich(chart.explanation)])
        parts.append(r"\section{Esercizi svolti}")
        for i, exercise in enumerate(lesson.exercises, 1):
            label = "Esercizio dalla fonte" if exercise.origin == "source" else "Esercizio didattico creato"
            parts.extend([r"\subsection{" + f"{i}. {label}" + "}",
                          r"\begin{tcolorbox}[breakable,colback=light,colframe=accent,title=Consegna]",
                          rich(exercise.question), bullets(exercise.requested_points, True), r"\end{tcolorbox}"])
            for step in exercise.steps:
                parts.extend([r"\subsubsection*{" + heading(step.label) + "}", rich(step.explanation)])
                if step.math:
                    parts.append(display(step.math))
            parts.extend([r"\paragraph{Risposte ai punti richiesti}", bullets(exercise.answers, True),
                          r"\paragraph{Controlli sul risultato}", bullets(exercise.checks)])
        parts.extend([r"\section{Richiamo attivo}", "Prova a rispondere prima di consultare le soluzioni in appendice.",
                      bullets([q.question for q in lesson.recall], True), r"\section*{Cosa devi saper fare}", bullets(lesson.recap)])
        if lesson.uncertainties and not all(any(u in str(flag) for flag in flags) for u in lesson.uncertainties):
            parts.extend([r"\paragraph{Dubbi da chiarire}", bullets(lesson.uncertainties)])
    parts.extend([r"\appendix\chapter{Risposte al richiamo attivo}"])
    for lesson in lessons:
        parts.extend([r"\section{" + heading(lesson.title) + "}", bullets([q.answer for q in lesson.recall], True)])
    parts.extend([r"\chapter{Fonti e tracciabilità}"])
    for index, document in enumerate(documents, 1):
        parts.extend([r"\section*{D" + f"{index:03d}" + " - " + escape(document["filename"]) + "}",
                      f"Pagine: {document['pages']}. Impronta SHA-256:" + r"\par{\small\ttfamily " +
                      "\\allowbreak{}".join(document["sha256"][n:n+8] for n in range(0,64,8)) + "}\\par\n\n"])
    parts.append("Il rapporto di qualità e i file strutturati nel pacchetto sorgente consentono di risalire da ogni argomento alla pagina originale. Le eventuali pagine escluse sono motivate nel rapporto.")
    tex = template.replace("%%CONTENT%%", "\n".join(parts))
    (folder / "dispensa.tex").write_text(tex, encoding="utf-8")
    diagnostics = compile_tex(folder)
    with fitz.open(folder / "dispensa.pdf") as pdf:
        diagnostics["pages"] = len(pdf)
        if len(pdf) < 2:
            raise LatexError("Il documento compilato è incompleto")
        diagnostics["empty_pages"] = [i+1 for i, p in enumerate(pdf) if len(p.get_text().strip()) < 3 and not p.get_images()]
    return diagnostics


def render_pdf_pages(pdf: Path, target: Path) -> list[Path]:
    target.mkdir(exist_ok=True, parents=True)
    paths = []
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc, 1):
            path = target / f"pagina-{i:04d}.jpg"
            page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False, colorspace=fitz.csRGB).save(path, jpg_quality=90)
            paths.append(path)
    return paths


def source_archive(folder: Path, extra: list[Path]):
    with zipfile.ZipFile(folder / "sorgenti.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in [folder / "dispensa.tex", *sorted((folder / "assets").glob("*"))]:
            if path.is_file():
                archive.write(path, path.relative_to(folder))
        for path in extra:
            if path.is_file():
                archive.write(path, "audit/" + path.name)
