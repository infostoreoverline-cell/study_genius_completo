"""Real end-to-end Carnot example, through the application's upload/start/download API.

The input contains diagrams, declared data and questions, without a worked solution.
Requires ReportLab in addition to the application dependencies. --live spends API credit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "carnot"
WORK = ROOT / "tmp" / "carnot"
R, N, TH, TC, GAMMA, VA, VB = 8.314, 1.0, 600.0, 300.0, 5 / 3, 10.0, 20.0

BRIEF = """Ciclo di Carnot per un gas ideale monoatomico: preparazione a un esame orale e scritto.
Voglio poter ricostruire e spiegare entrambi i grafici p-V e T-S senza la slide davanti.
Scomponi esplicitamente ciascun grafico nei quattro tratti A-B, B-C, C-D, D-A, mantenendo
le lettere della fonte. Per OGNI tratto spiega stato iniziale/finale, verso, grandezze costanti
e variabili, scambio di calore, lavoro, energia interna ed entropia, legge matematica e
perche la curva ha quella forma. Collega il gas/pistone/sorgente al grafico.
Mostra i passaggi delle derivate delle curve e confronta isoterma e adiabatica NELLO STESSO
stato. Giustifica perche T-S e rettangolare e p-V no; distingui le aree nei due diagrammi.
Risolvi integralmente tutti i quesiti della fonte con numeri, unita, segni e controlli.
Usa la convenzione chimica: lavoro ricevuto dal sistema w positivo, Delta U = q+w;
definisci separatamente W_out = -w. Dedica una sottosezione riconoscibile a ciascun tratto.
Le integrazioni didattiche sono benvenute se dichiarate come tali. Non attribuire alla fonte
spiegazioni che non contiene. Niente riempitivi o ripetizioni integrali tra capitoli.
Ogni formula deve avere unita in carattere dritto, spazi sottili tra valori e unita e cdot
per i prodotti. Ogni domanda di richiamo deve avere una risposta motivata in appendice.
"""


def prepare_source() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    ratio = (TH / TC) ** (1 / (GAMMA - 1))
    vd, vc = VA * ratio, VB * ratio
    ds = N * R * math.log(VB / VA)
    states = [("A", VA, TH, 0), ("B", VB, TH, ds), ("C", vc, TC, ds), ("D", vd, TC, 0)]
    phase_colors = ["#167a80", "#b65f39", "#566eac", "#54825a"]
    volumes = [np.linspace(VA, VB, 160), np.linspace(VB, vc, 160),
               np.linspace(vc, vd, 160), np.linspace(vd, VA, 160)]
    pressures = [N * R * TH / volumes[0],
                 N * R * TH / VB * (VB / volumes[1]) ** GAMMA,
                 N * R * TC / volumes[2],
                 N * R * TC / vd * (vd / volumes[3]) ** GAMMA]
    labels = ["A-B · Isoterma calda", "B-C · Adiabatica", "C-D · Isoterma fredda", "D-A · Adiabatica"]
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 11, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.titleweight": "bold"}):
        fig, ax = plt.subplots(figsize=(8, 5.4), layout="constrained")
        ax.fill(np.concatenate(volumes), np.concatenate(pressures), color="#e9efed", zorder=0)
        for v, p, color, label in zip(volumes, pressures, phase_colors, labels):
            ax.plot(v, p, color=color, linewidth=2.8, label=label)
            ax.annotate("", xy=(v[88], p[88]), xytext=(v[67], p[67]),
                        arrowprops={"arrowstyle": "-|>", "color": color, "lw": 2, "mutation_scale": 17})
        offsets = [(-15, 10), (10, 8), (7, -15), (-17, -14)]
        for (name, volume, temp, _), offset in zip(states, offsets):
            pressure = N * R * temp / volume
            ax.scatter([volume], [pressure], s=35, color="#213b3a", zorder=4)
            ax.annotate(name, (volume, pressure), textcoords="offset points", xytext=offset, weight="bold", fontsize=13)
        ax.set(xlabel="Volume V (L)", ylabel="Pressione p (kPa)", xlim=(0, 64), ylim=(0, 555), title="Il ciclo nel piano pressione-volume")
        ax.grid(alpha=.17)
        ax.legend(loc="upper right", frameon=True, framealpha=.96, fontsize=10)
        fig.savefig(WORK / "carnot-pv.png", dpi=180)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(8, 4.7), layout="constrained")
        ax.fill([0, ds, ds, 0], [TH, TH, TC, TC], color="#e9efed", zorder=0)
        coords = [(0, TH), (ds, TH), (ds, TC), (0, TC), (0, TH)]
        for i, color in enumerate(phase_colors):
            a, b = np.array(coords[i]), np.array(coords[i + 1])
            ax.plot([a[0], b[0]], [a[1], b[1]], color=color, linewidth=2.8)
            ax.annotate("", xy=a + .61 * (b - a), xytext=a + .44 * (b - a),
                        arrowprops={"arrowstyle": "-|>", "color": color, "lw": 2, "mutation_scale": 17})
        for (name, _, _, _), (sx, ty), offset in zip(states, coords, [(-17, 10), (7, 10), (7, -16), (-17, -16)]):
            ax.scatter([sx], [ty], s=35, color="#213b3a", zorder=4)
            ax.annotate(name, (sx, ty), textcoords="offset points", xytext=offset, weight="bold", fontsize=13)
        ax.set(xlabel="Entropia relativa S - S_A (J/K)", ylabel="Temperatura T (K)",
               xlim=(-.6, ds + .7), ylim=(220, 690), title="Gli stessi quattro stati nel piano temperatura-entropia")
        ax.grid(alpha=.17)
        fig.savefig(WORK / "carnot-ts.png", dpi=180)
        plt.close(fig)

    fonts = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    for name, file in [("StudySans", "DejaVuSans.ttf"), ("StudySans-Bold", "DejaVuSans-Bold.ttf")]:
        pdfmetrics.registerFont(TTFont(name, str(fonts / file)))
    styles = getSampleStyleSheet()
    styles["Heading3"].fontName = "StudySans-Bold"
    styles.add(ParagraphStyle(name="CourseTitle", fontName="StudySans-Bold", fontSize=24, leading=29,
                              textColor=colors.HexColor("#203c3b"), spaceAfter=16))
    styles.add(ParagraphStyle(name="CourseBody", fontName="StudySans", fontSize=10.5, leading=15,
                              textColor=colors.HexColor("#203c3b"), spaceAfter=9))
    styles.add(ParagraphStyle(name="CourseSmall", fontName="StudySans", fontSize=8.5, leading=12,
                              textColor=colors.HexColor("#63716b"), spaceAfter=7))
    def para(text, style="CourseBody"):
        return Paragraph(text, styles[style])
    story = [para("CICLO DI CARNOT", "CourseTitle"),
        para("Scheda sintetica di partenza per una prova di StudyGenius. Contiene grafici, dati e quesiti; non contiene uno svolgimento."),
        para("Modello: una mole di gas ideale monoatomico; tutte le trasformazioni sono reversibili. Capacita termiche costanti: C<sub>V,m</sub> = 3R/2, C<sub>p,m</sub> = 5R/2, gamma = 5/3."),
        Image(str(WORK / "carnot-pv.png"), width=168*mm, height=113.4*mm),
        para("Percorso del motore: A - B - C - D - A. Sorgente calda a 600 K, sorgente fredda a 300 K. V<sub>A</sub> = 10 L e V<sub>B</sub> = 20 L."),
        para("Relazioni disponibili: pV = nRT; lungo un'adiabatica reversibile pV<super>gamma</super> = costante. R = 8,314 J mol<super>-1</super> K<super>-1</super>; 1 kPa L = 1 J."),
        para("Il diagramma e calcolato analiticamente dalle equazioni dichiarate. Numeri scelti per l'esercizio: non sono misure sperimentali.", "CourseSmall"), PageBreak(),
        para("Un secondo sguardo allo stesso ciclo", "CourseTitle"),
        para("Nel diagramma seguente gli stati A, B, C e D corrispondono agli stessi stati della pagina precedente. L'origine orizzontale e un riferimento relativo: non si assume entropia assoluta nulla."),
        Image(str(WORK / "carnot-ts.png"), width=168*mm, height=98.7*mm),
        para("Per trasformazioni reversibili: delta q<sub>rev</sub> = T dS. Il ciclo ha due isoterme e due adiabatiche reversibili. Le frecce mostrano il verso di percorrenza."),
        para("Convenzione da usare: q positivo se ricevuto dal gas; w positivo se il lavoro e ricevuto dal gas. Primo principio: Delta U = q + w. Il lavoro erogato dal motore si indica separatamente con W<sub>out</sub> = -w."),
        para("Da discutere: significato dei lati orizzontali e verticali; significato delle aree nei due piani; scambi con le sorgenti; ritorno allo stato iniziale."), PageBreak(),
        para("Dati e quesiti d'esame", "CourseTitle")]
    rows = [["Stato", "V (L)", "p (kPa)", "T (K)", "S - S_A (J/K)"]]
    for name, volume, temp, entropy in states:
        rows.append([name, f"{volume:.6f}", f"{N*R*temp/volume:.6f}", f"{temp:.0f}", f"{entropy:.6f}"])
    table = Table(rows, colWidths=[20*mm, 37*mm, 37*mm, 27*mm, 47*mm])
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#167a80")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("FONTNAME", (0,0), (-1,-1), "StudySans"), ("FONTNAME", (0,0), (-1,0), "StudySans-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9), ("BOTTOMPADDING", (0,0), (-1,-1), 9),
        ("TOPPADDING", (0,0), (-1,-1), 9), ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#f0f6f4"), colors.white]),
        ("LINEBELOW", (0,-1), (-1,-1), .5, colors.HexColor("#c8d9d1"))]))
    story += [table, Spacer(1, 8*mm), para("Valori arrotondati in tabella. Per i calcoli usare i dati esatti del modello della prima pagina.", "CourseSmall")]
    questions = [
        "1. Spiegare separatamente A-B, B-C, C-D e D-A: cosa accade al gas, cosa resta costante, quali grandezze cambiano e perche il tratto ha quella forma in entrambi i diagrammi.",
        "2. Ricavare le espressioni p(V) delle isoterme e delle adiabatiche, derivarle rispetto a V e confrontarne le pendenze nello stesso stato. Ricavare V_C e V_D senza leggerli dal grafico.",
        "3. Calcolare per ogni tratto q, w, Delta U e Delta S, mostrando integrali, estremi, sostituzioni e segni. Distinguere sistema e ambiente.",
        "4. Calcolare W_out e il rendimento. Spiegare il significato delle aree racchiuse nei due grafici. Verificare la chiusura dei bilanci di energia ed entropia sull'intero ciclo.",
        "5. Spiegare che cosa cambierebbe invertendo il verso del ciclo e perche il ritorno allo stato iniziale non implica lavoro netto nullo."]
    story += [para(q) for q in questions]
    story += [Spacer(1, 5*mm), para("Approfondimenti consultati per l'impostazione generale", "Heading3"),
        para('<link href="https://web.mit.edu/16.unified/www/FALL/thermodynamics/notes/node24.html">MIT - Thermodynamics and Propulsion, 3.3 The Carnot Cycle</link>', "CourseSmall"),
        para('<link href="https://www.grc.nasa.gov/www/k-12/airplane/carnot.html">NASA Glenn - Carnot Cycle</link>', "CourseSmall"),
        para("Scheda originale e dati didattici preparati per questo collaudo. Le spiegazioni estese e le soluzioni devono essere prodotte dai modelli.", "CourseSmall")]
    def footer(canvas, doc):
        canvas.setFont("StudySans", 8)
        canvas.setFillColor(colors.HexColor("#63716b"))
        canvas.drawString(21*mm, 14*mm, "StudyGenius | Fonte sintetica - Ciclo di Carnot")
        canvas.drawRightString(189*mm, 14*mm, str(doc.page))
    source = OUT / "carnot-fonte.pdf"
    SimpleDocTemplate(str(source), pagesize=A4, leftMargin=21*mm, rightMargin=21*mm,
                      topMargin=20*mm, bottomMargin=22*mm, title="Ciclo di Carnot - fonte del collaudo").build(story, onFirstPage=footer, onLaterPages=footer)
    # Numerical reference is kept outside the input PDF and never passed to the models.
    cv = 1.5*R
    reference = {"V_C_L": vc, "V_D_L": vd, "Delta_S_AB_J_K": ds,
                 "q_J": [TH*ds, 0, -TC*ds, 0], "w_J": [-TH*ds, N*cv*(TC-TH), TC*ds, N*cv*(TH-TC)],
                 "Delta_U_J": [0, N*cv*(TC-TH), 0, N*cv*(TH-TC)],
                 "Delta_S_J_K": [ds, 0, -ds, 0], "W_out_J": (TH-TC)*ds, "efficiency": 1-TC/TH}
    (WORK / "numeric-reference.json").write_text(json.dumps(reference, indent=2), encoding="utf-8")
    print("SOURCE", source, flush=True)
    return source


async def run(args):
    source = OUT / "carnot-fonte.pdf"
    if not args.resume:
        source = prepare_source()
    if not args.live:
        print("Fonte preparata. Per la prova API a pagamento aggiungi --live.")
        return 0
    from studygenius.app import create_app
    from studygenius.models import JobOptions
    app = create_app(ROOT / ".studygenius" / "carnot-collaudo")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765", trust_env=False) as client:
            session = (await client.get("/api/session")).json()
            client.headers["X-StudyGenius-Token"] = session["token"]
            if args.resume:
                job_id = args.resume
            else:
                options = JobOptions(title="Il ciclo di Carnot - Comprendere ogni tratto", exam_brief=BRIEF,
                                     review_rounds=2, pages_per_batch=1,
                                     max_api_calls=args.max_api_calls or 45,
                                     max_total_tokens=args.max_total_tokens or 400000)
                response = await client.post("/api/jobs", data={"options": options.model_dump_json()},
                    files=[("files", (source.name, source.read_bytes(), "application/pdf"))])
                response.raise_for_status()
                job_id = response.json()["id"]
            (WORK / "live-job.json").write_text(json.dumps({"id": job_id}), encoding="utf-8")
            print("LIVE_JOB", job_id, flush=True)
            response = await client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            if job["status"] not in ("completed", "needs_review"):
                if args.resume and (args.max_api_calls or args.max_total_tokens):
                    response = await client.post(f"/api/jobs/{job_id}/limits", json={
                        "max_api_calls": args.max_api_calls or job["options"]["max_api_calls"],
                        "max_total_tokens": args.max_total_tokens or job["options"]["max_total_tokens"]})
                    response.raise_for_status()
                response = await client.post(f"/api/jobs/{job_id}/start")
                if response.status_code != 200:
                    print("START_ERROR", response.json().get("detail"), flush=True)
                    return 1
            previous = None
            while True:
                job = (await client.get(f"/api/jobs/{job_id}")).json()
                state = (job["stage"], job["usage"]["calls"], job["usage"]["total_tokens"])
                if state != previous:
                    print("STAGE", *state, sep=" | ", flush=True)
                    previous = state
                if job["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(3)
            if job["status"] not in ("completed", "needs_review"):
                print("LIVE_ERROR", job["status"], job["error"], flush=True)
                return 1
            for name, target in [("dispensa.pdf", "carnot-dispensa.pdf"), ("sorgenti.zip", "carnot-sorgenti.zip"),
                                 ("qualita.json", "carnot-qualita.json"), ("qualita.md", "carnot-qualita.md")]:
                response = await client.get(f"/api/jobs/{job_id}/download/{name}")
                response.raise_for_status()
                (OUT / target).write_bytes(response.content)
            print("LIVE_RESULT", job["status"], json.dumps(job["usage"]), flush=True)
            print("PDF", OUT / "carnot-dispensa.pdf", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Esegue chiamate API reali a pagamento")
    parser.add_argument("--resume", help="Riprende un lavoro interrotto senza ricreare la fonte")
    parser.add_argument("--max-api-calls", type=int, help="Limite di richieste incluse le riprese")
    parser.add_argument("--max-total-tokens", type=int, help="Limite di token confermati incluse le riprese")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
