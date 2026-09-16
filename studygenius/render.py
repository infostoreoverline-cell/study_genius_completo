"""Trusted LaTeX composition. Only whitelisted math reaches TeX; plots use numeric data."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
import uuid
import zipfile
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


from .models import (ArcoMappa, AsseGrafico, CurvaAnalitica, Lesson, NodoMappa,
                     SchedaAnaliticaGrafico, SchedaMappa, SourceVisual)
from .renderers import (graphviz_engine, render_analytic_chart_pdf,
                        render_concept_map_pdf)

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
langle rangle lbrace rbrace ldots cdots vdots ddots dots wedge vee quad qquad hspace phantom
displaystyle textstyle scriptstyle scriptscriptstyle binom dbinom tbinom overset underset
substack limits nolimits degree circ angle perp parallel ell hbar Re Im Pr
begin end boxed cancel ce SI si mbox mathsf mathscr numberwithin
qquad colon mid nonumber notag
""".split()) - {"hspace", "phantom", "ce", "SI", "si", "mathscr", "numberwithin", "cancel"}
MATH_ENVS = {"aligned", "gathered", "cases", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "smallmatrix"}
UNICODE_MATH = {"α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "δ": r"\delta ",
    "Δ": r"\Delta ", "ε": r"\varepsilon ", "η": r"\eta ", "θ": r"\theta ", "λ": r"\lambda ",
    "μ": r"\mu ", "π": r"\pi ", "ρ": r"\rho ", "σ": r"\sigma ", "τ": r"\tau ",
    "φ": r"\phi ", "ω": r"\omega ", "Ω": r"\Omega ", "∞": r"\infty ",
    "∂": r"\partial ", "∇": r"\nabla ", "∫": r"\int ", "≤": r"\leq ",
    "≥": r"\geq ", "≠": r"\neq ", "≈": r"\approx ", "×": r"\times ",
    "·": r"\cdot ", "−": "-", "→": r"\to ", "⇒": r"\Rightarrow ",
    "⇐": r"\Leftarrow ", "⇔": r"\Leftrightarrow ", "∮": r"\oint ", "°": r"^{\circ}"}


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
    # Braced decimal commas are TeX syntax. In a prose field the braces would be
    # printed literally, so normalize only the unambiguous digit form locally.
    value = re.sub(r"(?<=\d)\{,\}(?=\d)", ",", value)
    # Models often use calculator-style powers in otherwise plain prose. Turning
    # ``TV^(gamma-1)`` into a braced power here avoids printing a raw caret while
    # keeping the input inside the same mathematical whitelist used everywhere.
    value = re.sub(
        r"\b([A-Za-z][A-Za-z0-9]{0,3}(?:_(?:\{[^{}]{1,30}\}|[A-Za-z0-9]+))?)\^\(([^()\n]{1,40})\)",
        r"\1^{\2}", value)
    unicode_symbols = re.escape("".join(UNICODE_MATH))
    start = re.compile(r"\\{1,2}[A-Za-z]+|\b[A-Za-z]{1,3}(?=[_^])|\b\d{1,3}(?=\^)|[" + unicode_symbols + "]")
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
                operator_position = end
                end += 1
                if end < len(value) and value[end] != "{":
                    suffix = re.match(r"-?\d+|[A-Za-z0-9]+|\\{1,2}[A-Za-z]+|[" + unicode_symbols + "]", value[end:])
                    if suffix:
                        end += len(suffix.group())
                        continue
                    end = operator_position
                    break
                if end == len(value):
                    end = operator_position
                    break
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
        # Attach a following single-letter variable, including its indices, to a
        # Greek/operator command (for example ``\Delta U_AB``). Without the
        # suffix the underscore would be escaped and printed literally.
        if token.startswith("\\") or token in UNICODE_MATH:
            tail = re.match(r" ?[A-Za-z](?:[_^](?:\{[^{}]{1,40}\}|[A-Za-z0-9]+))*(?![A-Za-z0-9])", value[end:])
            if tail:
                end += len(tail.group())
        candidate = value[match.start():end]
        # Bare alphabetic multi-letter indices (p_ext) denote a label, not e*x*t.
        candidate = re.sub(r"([_^])([A-Za-z]{2,}|\d{2,})(?![A-Za-z0-9])", r"\1{\2}", candidate)
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


def _split_top_level(value: str, separators: tuple[str, ...]) -> list[str]:
    """Split TeX only outside braced groups; used for safe display line breaks."""
    parts, start, depth, cursor = [], 0, 0, 0
    while cursor < len(value):
        char = value[cursor]
        if char == "{" and (cursor == 0 or value[cursor - 1] != "\\"):
            depth += 1
        elif char == "}" and (cursor == 0 or value[cursor - 1] != "\\"):
            depth = max(0, depth - 1)
        if depth == 0:
            separator = next((item for item in separators if value.startswith(item, cursor)), None)
            if separator:
                part = value[start:cursor].strip()
                if part:
                    parts.append(part)
                cursor += len(separator)
                start = cursor
                continue
        cursor += 1
    tail = value[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def wrap_display_math(value: str, target_length: int = 105) -> str:
    """Break long equations before adjustbox would shrink them to tiny text.

    Semicolon/comma-separated balances are independent mathematical statements.
    Equality chains can continue on a new centered row without changing meaning.
    Existing explicit math environments are preserved verbatim.
    """
    value = safe_math(value)
    if len(value) <= target_length or r"\begin{" in value:
        return value
    components = _split_top_level(value, (r";\qquad", r";\quad", r",\qquad", r",\quad", ";", r"\qquad"))
    lines: list[str] = []
    for component in components:
        equalities = _split_top_level(component, ("=",))
        if len(component) > target_length and len(equalities) >= 3:
            lines.append(equalities[0] + "=" + equalities[1])
            lines.extend("{}=" + item for item in equalities[2:])
        else:
            lines.append(component)
    if len(lines) < 2:
        return value
    wrapped = r"\begin{gathered}" + r"\\".join(lines) + r"\end{gathered}"
    return safe_math(wrapped)


def display(value: str) -> str:
    return (r"\begin{center}\begin{adjustbox}{max width=\linewidth}$\displaystyle "
            + wrap_display_math(value) + r"$\end{adjustbox}\end{center}" + "\n")


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


def plot_text(value: str) -> str:
    """Render simple indices in plots without passing model-authored TeX through."""
    value = value.replace("$", "")
    value = re.sub(r"(?<=\d)\{,\}(?=\d)", ",", value)
    replacements = {r"\Delta": "Δ", r"\gamma": "γ", r"\eta": "η", r"\theta": "θ", r"\mu": "μ"}
    for source, replacement in replacements.items():
        value = value.replace(source, replacement)
    value = re.sub(r"\\(?:text|mathrm)\{([^{}]*)\}", r"\1", value)
    value = value.replace("{", "").replace("}", "").replace("\\", "")
    value = re.sub(r"\b([A-Za-z])_([A-Za-z0-9]{1,12})\b",
                   lambda match: f"${match.group(1)}_{{\\mathrm{{{match.group(2)}}}}}$", value)
    return value


def render_charts(lesson: Lesson, assets: Path, prefix: str) -> list[Path]:
    """Render author-created numeric charts as vector PDF plus review PNG."""
    assets.mkdir(exist_ok=True, parents=True)
    paths = []
    for index, chart in enumerate(lesson.charts, 1):
        card = SchedaAnaliticaGrafico(
            x_axis=AsseGrafico(label=chart.xlabel[:25]),
            y_axis=AsseGrafico(label=chart.ylabel[:25]),
            curves=[CurvaAnalitica(
                label=series.label or f"Serie {number}",
                x=series.x, y=series.y, interpretation=chart.explanation,
            ) for number, series in enumerate(chart.series, 1)],
            source_basis="tabulated_data",
            physical_chemical_meaning=(chart.explanation if len(chart.explanation) >= 20
                                       else chart.explanation + " Interpretazione fisico-chimica."),
            analytical_steps=[chart.provenance],
            limitations="I segmenti collegano i dati dichiarati; non aggiungono misure assenti dalla fonte.",
        )
        base = assets / f"{prefix}-chart-{index:02d}"
        render_analytic_chart_pdf(card, base.with_suffix(".pdf"), base.with_suffix(".png"), chart.title)
        paths.append(base.with_suffix(".png"))
    return paths


def render_concept_maps(lesson: Lesson, assets: Path, prefix: str) -> list[Path]:
    """Render author-created maps with Graphviz from validated nodes and edges."""
    assets.mkdir(exist_ok=True, parents=True)
    paths = []
    for index, concept_map in enumerate(lesson.concept_maps, 1):
        card = SchedaMappa(
            nodes=[NodoMappa.model_validate(node.model_dump()) for node in concept_map.nodes],
            edges=[ArcoMappa.model_validate(edge.model_dump()) for edge in concept_map.edges],
            reading_path=concept_map.reading_path,
            explanation=concept_map.explanation,
        )
        base = assets / f"{prefix}-map-{index:02d}"
        render_concept_map_pdf(card, base.with_suffix(".pdf"), base.with_suffix(".png"), concept_map.title)
        paths.append(base.with_suffix(".png"))
    return paths


def render_source_visual(visual: SourceVisual, assets: Path, prefix: str) -> dict:
    """Create the publication PDF and a disposable PNG used only by Gemini review."""
    assets.mkdir(exist_ok=True, parents=True)
    base = assets / prefix
    pdf, preview = base.with_suffix(".pdf"), base.with_suffix(".png")
    if visual.kind == "chart":
        render_analytic_chart_pdf(visual.chart, pdf, preview, visual.title)
    else:
        render_concept_map_pdf(visual.concept_map, pdf, preview, visual.title)
    return {"path": str(pdf), "review_path": str(preview), "schema": visual.model_dump(mode="json")}


def latex_engine() -> str | None:
    return next((shutil.which(name) for name in ("xelatex", "lualatex") if shutil.which(name)), None)


class LatexError(RuntimeError):
    pass


def latex_failure_detail(folder: Path, stdout: str) -> str:
    """Extract the first actionable compiler error and its local source lines."""
    log_path = folder / "dispensa.log"
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    combined = stdout + "\n" + log
    lines = combined.splitlines()
    errors = [index for index, line in enumerate(lines)
              if "dispensa.tex:" in line or line.startswith("!")]
    start = errors[0] if errors else max(0, len(lines) - 18)
    diagnostic = "\n".join(lines[max(0, start - 1):start + 9])
    match = re.search(r"dispensa\.tex:(\d+):", diagnostic)
    if match:
        source = (folder / "dispensa.tex").read_text(encoding="utf-8", errors="replace").splitlines()
        line_number = int(match.group(1))
        excerpt = []
        for number in range(max(1, line_number - 2), min(len(source), line_number + 2) + 1):
            excerpt.append(f"{number}: {source[number - 1][:500]}")
        diagnostic += "\nContesto locale:\n" + "\n".join(excerpt)
    return diagnostic[-3500:]


def compile_tex(folder: Path) -> dict:
    engine = latex_engine()
    if not engine:
        raise LatexError("Manca LaTeX: installa MiKTeX (Windows) o TeX Live con XeLaTeX. Vedi README.")
    target = folder / "dispensa.pdf"
    # Interrupted TeX runs can leave broken .aux files. All passes run in a
    # fresh output directory; the validated PDF replaces the public file in one
    # atomic operation, so downloads can never observe a partial xref table.
    staging = folder / f".latex-build-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    candidate = staging / "dispensa.pdf"
    shutil.copy2(folder / "dispensa.tex", staging / "dispensa.tex")
    assets = folder / "assets"
    if assets.is_dir():
        try:
            os.symlink(assets.resolve(), staging / "assets", target_is_directory=True)
        except OSError:  # Windows may require elevated symlink privileges.
            shutil.copytree(assets, staging / "assets")
    env = os.environ.copy()
    env.update({"openin_any": "p", "openout_any": "p", "shell_escape": "f"})
    # No API keys are passed to the compiler process.
    for name in list(env):
        if any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            env.pop(name)
    try:
        previous_signature = None
        for pass_index in range(4):
            try:
                done = subprocess.run([engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
                                       "-file-line-error", "dispensa.tex"],
                                      cwd=staging, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=180)
            except subprocess.TimeoutExpired:
                raise LatexError("Compilazione LaTeX oltre 180 secondi: verifica installazione e pacchetti.") from None
            if done.returncode:
                if (staging / "dispensa.log").is_file():
                    shutil.copy2(staging / "dispensa.log", folder / "dispensa.log")
                detail = latex_failure_detail(folder, done.stdout.decode("utf-8", errors="replace"))
                raise LatexError("Compilazione LaTeX fallita: " + detail)
            # A long table of contents can change its own page count. Two fixed
            # passes are then insufficient and leave every following page off by
            # one. Stop as soon as references converge, with four local passes as
            # a hard bound; this costs no API tokens.
            signature = b"".join((staging / f"dispensa.{suffix}").read_bytes()
                                 for suffix in ("aux", "toc", "out")
                                 if (staging / f"dispensa.{suffix}").is_file())
            if pass_index >= 1 and signature == previous_signature:
                break
            previous_signature = signature
        if not candidate.is_file() or b"%%EOF" not in candidate.read_bytes()[-4096:]:
            raise LatexError("LaTeX non ha prodotto un PDF completo")
        try:
            with fitz.open(candidate) as document:
                if len(document) < 1:
                    raise LatexError("LaTeX ha prodotto un PDF privo di pagine")
        except (fitz.FileDataError, RuntimeError):
            raise LatexError("LaTeX ha prodotto un PDF non leggibile") from None
        log = (staging / "dispensa.log").read_text(errors="replace")
        os.replace(candidate, target)
        for suffix in ("aux", "log", "out", "toc"):
            generated = staging / f"dispensa.{suffix}"
            if generated.is_file():
                os.replace(generated, folder / generated.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    overflow = [float(x) for x in re.findall(r"Overfull \\[hv]box \(([\d.]+)pt too (?:wide|high)\)", log)]
    missing_chars = list(dict.fromkeys(re.findall(r"Missing character: (.+)", log)))
    return {"engine": Path(engine).name, "overfull_boxes": [v for v in overflow if v > 2], "missing_characters": missing_chars}


def preflight_latex(folder: Path) -> None:
    """Fail before spending API credit if TeX or Graphviz is unavailable."""
    if not graphviz_engine():
        raise LatexError("Manca Graphviz: installalo e aggiungi il comando dot al PATH. Vedi README.")
    folder.mkdir(parents=True, exist_ok=True)
    template = (Path(__file__).parent / "templates" / "book.tex").read_text(encoding="utf-8")
    content = (r"\chapter*{Verifica locale}Testo italiano: perché, quantità, energia. "
               r"\begin{tcolorbox}Formula di controllo: \(pV=nRT\).\end{tcolorbox}")
    (folder / "dispensa.tex").write_text(template.replace("%%CONTENT%%", content), encoding="utf-8")
    diagnostics = compile_tex(folder)
    if diagnostics["missing_characters"]:
        raise LatexError("L'installazione LaTeX non dispone dei glifi necessari: verifica i font.")



def source_visual_box(source: SourceVisual) -> str:
    parts = [r"\begin{tcolorbox}[breakable,colback=light,colframe=accent,title=Scomposizione analitica]"]
    if source.kind == "chart":
        card = source.chart
        x_axis = card.x_axis.label + (f" ({card.x_axis.unit})" if card.x_axis.unit else "")
        y_axis = card.y_axis.label + (f" ({card.y_axis.unit})" if card.y_axis.unit else "")
        parts.extend([
            r"\textbf{Significato fisico/chimico.} " + rich(card.physical_chemical_meaning),
            r"\textbf{Assi.} " + escape(f"x: {x_axis}, scala {card.x_axis.scale}; y: {y_axis}, scala {card.y_axis.scale}."),
        ])
        for curve in card.curves:
            parts.append(r"\paragraph{Curva: " + heading(curve.label) + "}")
            if curve.formula_latex:
                parts.append(display(curve.formula_latex))
            if curve.parameters:
                parts.append(r"\textbf{Parametri.} " + bullets(
                    [f"{name} = {value:g}" for name, value in curve.parameters.items()]))
            parts.append(rich(curve.interpretation))
        if card.critical_points:
            parts.extend([r"\textbf{Punti chiave.}", bullets([
                f"{point.label}: ({point.x:g}, {point.y:g}). {point.description}"
                for point in card.critical_points
            ])])
        parts.extend([r"\textbf{Passaggi analitici.}", bullets(card.analytical_steps, True),
                      r"\textbf{Limiti.} " + rich(card.limitations)])
    else:
        card = source.concept_map
        parts.extend([r"\textbf{Percorso di lettura.}", bullets(card.reading_path, True),
                      r"\textbf{Relazioni rappresentate.} " + rich(card.explanation)])
    if source.uncertainty:
        parts.append(r"\textbf{Incertezza dichiarata.} " + rich(source.uncertainty))
    parts.append(r"\end{tcolorbox}")
    return "\n".join(parts)

def build_book(folder: Path, title: str, plans: list[dict], lessons: list[Lesson], visual_assets: dict,
               topic_refs: dict, documents: list[dict], report: dict, mode="live") -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    template = (Path(__file__).parent / "templates" / "book.tex").read_text(encoding="utf-8")
    flags = report.get("issues", [])
    status = "DIMOSTRAZIONE OFFLINE" if mode == "demo" else ("DA VERIFICARE" if flags else "REVISIONI AUTOMATICHE COMPLETATE")
    parts = [r"\begin{titlepage}\sffamily", r"{\color{accent}\Large STUDYGENIUS}\par",
             r"\vspace{20mm}{\Huge\bfseries\raggedright\hyphenpenalty=10000\exhyphenpenalty=10000 " + escape(title) + r"\par}",
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
                parts.extend([r"\Needspace{18\baselineskip}\begin{tcolorbox}[breakable,colback=light,colframe=accent]", display(equation.latex),
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
            source = SourceVisual.model_validate(asset["schema"])
            parts.extend([r"\Needspace{18\baselineskip}\section*{Figura vettoriale: " + heading(asset["title"]) + "}",
                          r"\begin{figure}[htbp]",
                          r"\centering",
                          r"\includegraphics[width=0.85\linewidth]{assets/" + name + r"}",
                          r"\end{figure}",
                          r"{\small Ricostruzione vettoriale dalla fonte: " + escape(asset["reference"]) + r"}\par",
                          source_visual_box(source),
                          r"\paragraph{Come leggerla}", bullets(visual.how_to_read, True),
                          rich(visual.meaning), bullets(visual.takeaways),
                          r"\paragraph{Limiti di lettura} " + rich(visual.limitations)])
        render_concept_maps(lesson, folder / "assets", prefix)
        for i, concept_map in enumerate(lesson.concept_maps, 1):
            parts.extend([r"\Needspace{16\baselineskip}\section*{Mappa concettuale}",
                          r"\begin{center}\includegraphics[width=\linewidth,height=0.52\textheight,keepaspectratio]{assets/" + f"{prefix}-map-{i:02d}.pdf" + r"}\end{center}",
                          r"\paragraph{Percorso di lettura}", bullets(concept_map.reading_path, True),
                          rich(concept_map.explanation)])
        render_charts(lesson, folder / "assets", prefix)
        for i, chart in enumerate(lesson.charts, 1):
            parts.extend([r"\Needspace{16\baselineskip}\section*{Grafico ricostruito: " + heading(chart.title) + "}",
                          r"\begin{center}\includegraphics[width=\linewidth,height=0.52\textheight,keepaspectratio]{assets/" + f"{prefix}-chart-{i:02d}.pdf" + r"}\end{center}",
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


def inspect_pdf_layout(pdf: Path) -> dict:
    """Check every page locally and select pages needing full-resolution visual review."""
    actual_issues: list[str] = []
    detailed_pages: set[int] = set()
    flagged = []
    global_min_font = None
    with fitz.open(pdf) as document:
        for number, page in enumerate(document, 1):
            rect = page.rect
            dictionary = page.get_text("dict")
            sizes = []
            small_character_count = 0
            outside = 0
            image_blocks = 0
            has_heading = False
            for block in dictionary.get("blocks", []):
                if block.get("type") == 1:
                    image_blocks += 1
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if not span.get("text", "").strip():
                            continue
                        size = float(span.get("size", 0))
                        sizes.append(size)
                        if size < 7:
                            small_character_count += len(span.get("text", "").strip())
                        has_heading = has_heading or size >= 18
                        x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                        if x0 < rect.x0 - 1 or y0 < rect.y0 - 1 or x1 > rect.x1 + 1 or y1 > rect.y1 + 1:
                            outside += 1
            minimum = min(sizes) if sizes else None
            if minimum is not None:
                global_min_font = minimum if global_min_font is None else min(global_min_font, minimum)
            reasons = []
            if number in (1, len(document)):
                reasons.append("pagina iniziale/finale")
            if image_blocks or page.get_images(full=True):
                reasons.append("figure")
            if has_heading:
                reasons.append("inizio sezione/capitolo")
            # Normal-size mathematics contains small sub/superscript spans. Send
            # a page for detailed review only when the base text is likely small:
            # a very low minimum or a substantial amount of sub-7 pt content.
            if minimum is not None and (minimum < 5.5 or small_character_count >= 60):
                reasons.append("testo piccolo")
            if outside:
                reasons.append("testo oltre pagina")
                actual_issues.append(f"Pagina {number}: {outside} frammenti testuali oltre il riquadro PDF.")
            if minimum is not None and minimum < 5.5:
                actual_issues.append(f"Pagina {number}: corpo minimo {minimum:.1f} pt, potenzialmente illeggibile.")
            if reasons:
                detailed_pages.add(number)
                flagged.append({"page": number, "reasons": reasons,
                                "min_font_pt": round(minimum, 2) if minimum is not None else None,
                                "small_text_characters": small_character_count,
                                "image_blocks": image_blocks, "outside_spans": outside})
        total = len(document)
    return {"pages_checked": total, "minimum_font_pt": round(global_min_font, 2) if global_min_font else None,
            "issues": list(dict.fromkeys(actual_issues)), "detailed_pages": sorted(detailed_pages),
            "detailed_page_metrics": flagged}


def create_layout_review_assets(pdf: Path, target: Path, detailed_pages: list[int]) -> dict:
    """Create all-page contact sheets plus readable renders of higher-risk pages."""
    target.mkdir(exist_ok=True, parents=True)
    for stale in target.glob("*.jpg"):
        stale.unlink()
    overview, detail = [], []
    with fitz.open(pdf) as document:
        thumbs = []
        for number, page in enumerate(document, 1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(0.68, 0.68), alpha=False, colorspace=fitz.csRGB)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            thumbs.append((number, image))
            if number in detailed_pages:
                full = page.get_pixmap(matrix=fitz.Matrix(1.45, 1.45), alpha=False, colorspace=fitz.csRGB)
                full_image = Image.frombytes("RGB", (full.width, full.height), full.samples)
                path = target / f"dettaglio-pagina-{number:04d}.jpg"
                full_image.save(path, "JPEG", quality=86, optimize=True, progressive=True)
                detail.append(path)
        for start in range(0, len(thumbs), 9):
            group = thumbs[start:start + 9]
            cell_w = max(image.width for _, image in group) + 20
            cell_h = max(image.height for _, image in group) + 48
            sheet = Image.new("RGB", (cell_w * 3, cell_h * 3), "white")
            draw = ImageDraw.Draw(sheet)
            for position, (number, image) in enumerate(group):
                column, row = position % 3, position // 3
                x, y = column * cell_w + 10, row * cell_h + 34
                draw.text((x, row * cell_h + 10), f"Pagina {number}", fill="black")
                sheet.paste(image, (x, y))
            path = target / f"panoramica-{group[0][0]:04d}-{group[-1][0]:04d}.jpg"
            sheet.save(path, "JPEG", quality=82, optimize=True, progressive=True)
            overview.append(path)
    return {"overview": overview, "detail": detail}


def source_archive(folder: Path, extra: list[Path]):
    with zipfile.ZipFile(folder / "sorgenti.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in [folder / "dispensa.tex", *sorted((folder / "assets").glob("*"))]:
            if path.is_file():
                archive.write(path, path.relative_to(folder))
        for path in extra:
            if path.is_file():
                archive.write(path, "audit/" + path.name)
