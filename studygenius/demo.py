"""A deterministic fixture, explicitly labelled. Never used as a fallback for real PDFs."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import fitz

from .ingest import ingest
from .models import ChapterPlan, Lesson
from .render import build_book, render_charts, validate_lesson_math
from .storage import atomic_json


def demo_content() -> tuple[ChapterPlan, Lesson]:
    t1, t2 = "D001-P0001-T01", "D001-P0002-T01"
    plan = ChapterPlan(title="Gas ideale: dall'equazione di stato al lavoro", topic_ids=[t1,t2],
        objectives=["Interpretare una curva isoterma nel piano pressione-volume.",
                    "Calcolare il lavoro reversibile esplicitando segni, unità e integrali.",
                    "Collegare lavoro, calore ed energia interna con il primo principio."],
        prerequisites=["Concetto di pressione e volume; logaritmo naturale; integrale di 1/x."])
    lesson = Lesson.model_validate({
      "title":plan.title,
      "introduction":"Un gas che si espande può trasferire energia all'ambiente spostando un pistone. Per quantificare questo trasferimento servono tre elementi: il modello del gas, il percorso della trasformazione e una convenzione coerente per il segno del lavoro. In questo capitolo li costruiamo uno alla volta, usando un'espansione isoterma come esempio.",
      "sections":[{
        "title":"Descrivere lo stato di un gas ideale", "topic_ids":[t1],
        "paragraphs":[
          "Il modello di gas ideale trascura il volume proprio delle particelle e le interazioni intermolecolari, salvo gli urti. La sua utilità è collegare pressione, volume, temperatura e quantità di sostanza mediante un'equazione semplice. È un'approssimazione: vicino alla condensazione o ad alta densità può risultare inadeguata.",
          "La pressione misura la forza normale esercitata per unità di superficie. La temperatura da inserire nell'equazione è sempre quella assoluta in kelvin. Fissando la quantità di gas e la temperatura, il prodotto tra pressione e volume rimane costante: se il volume raddoppia, la pressione si dimezza. Questa relazione descrive un'isoterma, cioè l'insieme degli stati alla stessa temperatura."
        ],
        "equations":[{"latex":"pV=nRT", "explanation":"L'equazione di stato collega grandezze che descrivono uno stato di equilibrio. Non specifica da sola il processo seguito dal gas per passare a un altro stato.",
          "symbols":["$p$: pressione assoluta, in pascal (Pa).", "$V$: volume, in metri cubi ($\\mathrm{m^3}$).", "$n$: quantità di sostanza, in moli (mol).", "$T$: temperatura assoluta, in kelvin (K).", "$R=8.314\\,\\mathrm{J\\,mol^{-1}\\,K^{-1}}$: costante molare dei gas, arrotondata per questo esempio."],
          "assumptions":"Gas descritto dal modello ideale; grandezze macroscopiche riferite a stati di equilibrio."}],
        "derivation":[
          {"label":"Isolare la pressione", "explanation":"Dividiamo entrambi i membri per il volume, che è positivo. Per una mole a 300 K il numeratore è una costante.", "math":"p(V)=\\frac{nRT}{V}"},
          {"label":"Capire la pendenza", "explanation":"Deriviamo rispetto al volume mantenendo n e T costanti. Il segno negativo esprime la diminuzione della pressione durante l'espansione.", "math":"\\left(\\frac{\\partial p}{\\partial V}\\right)_T=-\\frac{nRT}{V^2}<0"}
        ],
        "pitfalls":["Usare la temperatura in gradi Celsius nell'equazione di stato.", "Mescolare litri e metri cubi senza convertire anche le unità di pressione e la costante R.", "Interpretare l'isoterma come una retta: la relazione è inversa, non lineare."]
      },{
        "title":"Calcolare il lavoro senza perdere il significato fisico", "topic_ids":[t2],
        "paragraphs":[
          "Adottiamo la convenzione chimica: il lavoro è positivo quando l'ambiente compie lavoro sul sistema. Durante un'espansione il sistema compie lavoro sull'ambiente, quindi il lavoro ricevuto dal gas è negativo. Il volume cambia di una quantità infinitesima e il lavoro elementare dipende dalla pressione esterna che si oppone al movimento.",
          "Nel limite reversibile, il processo attraversa una successione di stati di equilibrio e la pressione esterna differisce infinitesimamente da quella interna. Possiamo quindi usare la pressione del gas nell'integrale. Questo passaggio è essenziale: per una trasformazione irreversibile non basta conoscere l'equazione di stato per sostituire automaticamente la pressione esterna.",
          "Il lavoro dipende dal percorso. Al contrario, l'energia interna è una funzione di stato. Per un gas ideale di composizione fissa dipende soltanto dalla temperatura: in un processo isotermo la variazione di energia interna è nulla. Il calore scambiato compensa allora il lavoro."
        ],
        "equations":[{"latex":"\\delta w=-p_{\\mathrm{ext}}\\,dV", "explanation":"Il segno meno implementa la convenzione del lavoro ricevuto dal sistema. Durante un'espansione dV è positivo e quindi il lavoro elementare è negativo.", "symbols":["$\\delta w$: lavoro elementare ricevuto dal sistema, in joule.", "$p_{\\mathrm{ext}}$: pressione esterna, in pascal.", "$dV$: variazione infinitesima di volume, in metri cubi."], "assumptions":"Solo lavoro di espansione/compressione; pistone con forza esterna descrivibile tramite una pressione."},
          {"latex":"\\Delta U=q+w", "explanation":"Il primo principio esprime la conservazione dell'energia. q è positivo se il calore entra nel sistema; w è positivo se il lavoro viene compiuto sul sistema.", "symbols":["$\\Delta U$: variazione di energia interna, in joule.", "$q$: calore assorbito dal sistema, in joule.", "$w$: lavoro ricevuto dal sistema, in joule."], "assumptions":"Sistema chiuso; variazioni macroscopiche di energia cinetica e potenziale trascurabili."}],
        "derivation":[
          {"label":"Scegliere gli estremi", "explanation":"Sommiamo i contributi di lavoro dal volume iniziale al volume finale. Gli estremi descrivono precisamente il processo che stiamo studiando.", "math":"w=-\\int_{V_i}^{V_f}p_{\\mathrm{ext}}\\,dV"},
          {"label":"Applicare la reversibilità e il modello ideale", "explanation":"Solo nel limite reversibile sostituiamo la pressione esterna con nRT/V. n, R e T sono costanti lungo il processo isotermo, perciò escono dall'integrale.", "math":"w_{\\mathrm{rev}}=-\\int_{V_i}^{V_f}\\frac{nRT}{V}\\,dV=-nRT\\int_{V_i}^{V_f}\\frac{dV}{V}"},
          {"label":"Integrare una quantità adimensionale", "explanation":"Introduciamo x=V/Vi: allora dV=Vi dx e dV/V=dx/x. Gli estremi diventano 1 e Vf/Vi. Questa forma evita di applicare un logaritmo a un volume con dimensioni fisiche.", "math":"w_{\\mathrm{rev}}=-nRT\\int_1^{V_f/V_i}\\frac{dx}{x}=-nRT\\left[\\ln x\\right]_1^{V_f/V_i}"},
          {"label":"Valutare entrambi gli estremi", "explanation":"Il limite inferiore contribuisce con ln(1)=0. Il risultato dipende dal rapporto tra i volumi, che deve essere adimensionale.", "math":"w_{\\mathrm{rev}}=-nRT\\left(\\ln\\frac{V_f}{V_i}-\\ln 1\\right)=-nRT\\ln\\frac{V_f}{V_i}"}
        ],
        "pitfalls":["Sostituire la pressione interna a quella esterna senza verificare la reversibilità.", "Usare una pressione costante nell'integrale di un'isoterma reversibile.", "Confondere il lavoro ricevuto dal gas con quello compiuto dal gas: hanno segno opposto."]
      }],
      "exercises":[{
        "question":"Una mole di gas ideale a T = 300 K si espande reversibilmente e isotermicamente da Vi = 10.0 L a Vf = 20.0 L. Si trascurino le variazioni di energia cinetica e potenziale. Usare R = 8.314 J mol^-1 K^-1 e la convenzione chimica per il lavoro.",
        "origin":"source", "topic_ids":[t1,t2],
        "requested_points":["Calcolare la pressione iniziale e quella finale.", "Calcolare il lavoro ricevuto dal gas.", "Calcolare la variazione di energia interna e il calore assorbito."],
        "steps":[
          {"label":"Punto 1 - Convertire i volumi", "explanation":"Un litro equivale a un millesimo di metro cubo. Convertiamo entrambi i volumi prima di usare R nelle unità del Sistema Internazionale.", "math":"V_i=10.0\\times10^{-3}=0.0100\\,\\mathrm{m^3},\\qquad V_f=20.0\\times10^{-3}=0.0200\\,\\mathrm{m^3}"},
          {"label":"Punto 1 - Calcolare nRT", "explanation":"Moltiplichiamo quantità di sostanza, costante dei gas e temperatura. Moli e kelvin si semplificano; resta un'energia in joule.", "math":"nRT=(1.00)(8.314)(300)=2494.2\\,\\mathrm{J}"},
          {"label":"Punto 1 - Valutare le pressioni", "explanation":"Dividiamo lo stesso numeratore per il volume iniziale e poi per quello finale. Poiché un joule è un pascal per metro cubo, i risultati sono espressi in pascal.", "math":"\\begin{aligned}p_i&=\\frac{2494.2}{0.0100}=249420\\,\\mathrm{Pa}=249.42\\,\\mathrm{kPa}\\\\p_f&=\\frac{2494.2}{0.0200}=124710\\,\\mathrm{Pa}=124.71\\,\\mathrm{kPa}\\end{aligned}"},
          {"label":"Punto 2 - Costruire il rapporto dei volumi", "explanation":"I due volumi devono essere espressi nella stessa unità. Le unità si semplificano e il rapporto è due, un numero puro.", "math":"\\frac{V_f}{V_i}=\\frac{20.0\\,\\mathrm{L}}{10.0\\,\\mathrm{L}}=2.00"},
          {"label":"Punto 2 - Sostituire nella formula integrata", "explanation":"Usiamo il risultato derivato per il lavoro reversibile isotermo. Il logaritmo naturale di due è positivo e il segno esterno rende negativo il lavoro ricevuto dal gas.", "math":"w=-2494.2\\ln(2)\\,\\mathrm{J}=-2494.2(0.693147)\\,\\mathrm{J}\\approx-1728.85\\,\\mathrm{J}"},
          {"label":"Punto 3 - Valutare l'energia interna", "explanation":"Il gas è ideale, la quantità di sostanza non cambia e la temperatura rimane costante. Poiché U dipende solo da T, la variazione di energia interna è nulla.", "math":"\\Delta U=0"},
          {"label":"Punto 3 - Isolare il calore", "explanation":"Sostituiamo la variazione di energia interna nel primo principio e portiamo il lavoro all'altro membro. Il calore è positivo: il gas assorbe dall'ambiente l'energia che cede come lavoro.", "math":"0=q+w\\quad\\Rightarrow\\quad q=-w\\approx+1728.85\\,\\mathrm{J}"}
        ],
        "answers":["$p_i\\approx249\\,\\mathrm{kPa}$ e $p_f\\approx125\\,\\mathrm{kPa}$, a tre cifre significative.", "$w\\approx-1.73\\,\\mathrm{kJ}$: il gas compie lavoro sull'ambiente.", "$\\Delta U=0$ e $q\\approx+1.73\\,\\mathrm{kJ}$: il gas assorbe calore."],
        "checks":["Controllo dimensionale: $nRT$ ha dimensioni di energia, mentre il logaritmo è adimensionale.", "Controllo del segno: l'espansione ha $V_f>V_i$, quindi $w<0$ nella convenzione adottata.", "Controllo fisico: raddoppiando il volume a temperatura costante, la pressione finale deve essere metà di quella iniziale.", "Caso limite: se $V_f=V_i$, il logaritmo vale zero e non viene compiuto lavoro.", "Bilancio energetico: $q+w=0$, coerentemente con $\\Delta U=0$."]
      }],
      "recall":[
        {"question":"Perché il logaritmo nella formula del lavoro contiene un rapporto di volumi?", "answer":"L'argomento di un logaritmo deve essere adimensionale. Il cambio di variabile x=V/Vi produce l'integrale tra 1 e Vf/Vi; il rapporto elimina le unità."},
        {"question":"Un'espansione isoterma ha sempre lo stesso lavoro tra due volumi?", "answer":"No. Il lavoro dipende dal percorso e dalla pressione esterna. La formula con il logaritmo vale per il percorso reversibile del gas ideale, non per una qualunque espansione isoterma."},
        {"question":"Se la temperatura non cambia, perché il gas deve assorbire calore?", "answer":"Durante l'espansione reversibile il gas cede energia come lavoro. Per il gas ideale isotermo l'energia interna non cambia: il calore assorbito compensa il lavoro ceduto."},
        {"question":"Come cambierebbe il segno del lavoro per una compressione reversibile isoterma?", "answer":"In una compressione Vf/Vi è minore di uno e il suo logaritmo è negativo. Il segno meno davanti rende il lavoro positivo: l'ambiente compie lavoro sul gas."}
      ],
      "visuals":[{"visual_id":"D001-P0001-V01", "how_to_read":["L'asse orizzontale riporta il volume in litri; quello verticale la pressione in kilopascal.", "Ogni punto appartiene alla stessa temperatura, 300 K. Procedendo verso destra il volume cresce e la pressione diminuisce.", "Confronta gli estremi: passando da 10 L a 20 L, la pressione passa da circa 249 kPa a circa 125 kPa."], "meaning":"La curva rappresenta la relazione inversa tra pressione e volume. La sua area tra due volumi rappresenta il modulo del lavoro reversibile di espansione, usando unità coerenti: 1 kPa L = 1 J.", "takeaways":["La pendenza è negativa ma il suo modulo diminuisce all'aumentare del volume.", "L'area sotto la curva è positiva; il lavoro ricevuto dal gas è il suo opposto nella convenzione chimica."], "limitations":"La curva usa il modello ideale e non rappresenta effetti di gas reale. La lettura della pressione dal grafico è approssimata; il calcolo quantitativo usa l'equazione."}],
      "concept_maps":[{"title":"Dallo stato del gas al bilancio energetico", "topic_ids":[t1,t2],
        "nodes":[
          {"id":"stato", "label":"Stato del gas ideale", "detail":"pV = nRT", "kind":"law"},
          {"id":"percorso", "label":"Percorso isotermo reversibile", "detail":"T costante; p_ext ≈ p", "kind":"process"},
          {"id":"lavoro", "label":"Lavoro di espansione", "detail":"w = -∫p_ext dV", "kind":"concept"},
          {"id":"energia", "label":"Primo principio", "detail":"ΔU = q + w", "kind":"law"},
          {"id":"bilancio", "label":"Bilancio isotermo", "detail":"ΔU = 0, quindi q = -w", "kind":"example"}],
        "edges":[
          {"source":"stato", "target":"percorso", "label":"descrive gli stati", "kind":"explains"},
          {"source":"percorso", "target":"lavoro", "label":"determina", "kind":"leads_to"},
          {"source":"lavoro", "target":"energia", "label":"entra nel", "kind":"leads_to"},
          {"source":"energia", "target":"bilancio", "label":"con T costante", "kind":"leads_to"}],
        "reading_path":["Parti dall'equazione di stato, che collega le variabili macroscopiche.", "Specifica il percorso prima di calcolare il lavoro.", "Inserisci il lavoro nel primo principio e interpreta il bilancio isotermo."],
        "explanation":"La mappa separa tre livelli che spesso vengono confusi: l'equazione di stato descrive gli stati, il percorso rende calcolabile il lavoro e il primo principio chiude il bilancio energetico."}],
      "charts":[{"title":"Isoterma di una mole di gas ideale a 300 K", "topic_ids":[t1], "xlabel":"Volume V (L)", "ylabel":"Pressione p (kPa)", "kind":"line", "series":[{"label":"T = 300 K", "x":[10,12,14,16,18,20], "y":[249.42,207.85,178.157142857,155.8875,138.566666667,124.71]}], "provenance":"Ricostruzione della tabella numerica nella pagina 1 della fonte dimostrativa; p (kPa) = 2494.2 / V (L). I segmenti collegano i punti tabulati e non sono misurazioni sperimentali.", "explanation":"Il grafico ricostruito conserva assi, unità e valori della tabella. La linea tra i punti è un'interpolazione visiva; per ottenere valori intermedi accurati si usa l'equazione del gas ideale."}],
      "recap":["Saper distinguere equazione di stato e descrizione di una trasformazione.", "Motivare ogni sostituzione nella derivazione del lavoro reversibile.", "Controllare unità, segni e casi limite prima di accettare un risultato.", "Usare il primo principio con una convenzione di segno dichiarata."], "uncertainties":[]
    })
    return plan, lesson


def make_source(directory: Path, lesson: Lesson):
    images = render_charts(lesson, directory / "demo-plots", "source")
    target = directory / "inputs" / "demo.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((44,55), "FONTE DIMOSTRATIVA - CHIMICA FISICA", fontsize=16)
    page.insert_textbox(fitz.Rect(44,80,551,255),
        "Gas ideale: pV = nRT. A n e T costanti, p = nRT/V.\n"
        "Si usa n = 1 mol, T = 300 K, R = 8.314 J/(mol K).\n"
        "Tabella V (L) -> p (kPa):\n10 -> 249.42; 12 -> 207.85; 14 -> 178.15714;\n"
        "16 -> 155.8875; 18 -> 138.56667; 20 -> 124.71.\n"
        "Il modello trascura le interazioni intermolecolari e il volume proprio delle particelle.\n"
        "L'area sotto la curva p(V) misura il modulo del lavoro reversibile.", fontsize=11, lineheight=1.5)
    page.insert_image(fitz.Rect(35,280,560,640), filename=images[0], keep_proportion=True)
    page.insert_text((44,780), "Materiale sintetico di collaudo. Non proviene da un corso o da un esame reale.", fontsize=9)
    page = doc.new_page(width=595, height=842)
    page.insert_text((44,55), "LAVORO E PRIMO PRINCIPIO", fontsize=16)
    page.insert_textbox(fitz.Rect(44,85,550,380),
        "Convenzione chimica: il lavoro ricevuto dal sistema e' positivo.\n"
        "Lavoro elementare: delta w = -p_ext dV. Nel limite reversibile p_ext = p.\n"
        "Espansione isoterma reversibile: w = -nRT ln(Vf/Vi).\n"
        "Primo principio: Delta U = q + w. Per un gas ideale isotermo Delta U = 0 e q = -w.\n\n"
        + lesson.exercises[0].question + "\n\n"
        + "\n".join(f"{i+1}) {point}" for i,point in enumerate(lesson.exercises[0].requested_points)), fontsize=11, lineheight=1.6)
    doc.save(target)
    doc.close()
    atomic_json(directory / "inputs.json", [{"filename":"fonte-demo-gas-ideale.pdf", "stored_name":"demo.pdf",
                "pages":2, "sha256":hashlib.sha256(target.read_bytes()).hexdigest()}])


async def run_demo(pipeline):
    from .ingest import crop_visual
    from .models import SourceVisual
    from .storage import read_json
    plan, lesson = demo_content()
    validate_lesson_math(lesson)
    await asyncio.to_thread(make_source, pipeline.directory, lesson)
    pipeline.progress("Demo · lettura del PDF di esempio", 0.2, "Demo offline: uso contenuti didattici prestabiliti; nessuna API viene chiamata.")
    pages = await asyncio.to_thread(ingest, pipeline.directory, pipeline.check)
    atomic_json(pipeline.directory / "outline.json", [plan.model_dump()])
    target = pipeline.directory / "visuals" / "D001-P0001-V01.png"
    visual = SourceVisual(title="Isoterma del gas ideale", bbox=[50,320,950,780], description="Pressione in funzione del volume, a temperatura costante.")
    await asyncio.to_thread(crop_visual, pipeline.directory, pages[0], visual, target)
    assets = {"D001-P0001-V01":{"path":str(target),"title":visual.title,"reference":"D001, p. 1"}}
    pipeline.progress("Demo · composizione LaTeX", 0.6, "Compilo realmente il documento con LaTeX.")
    report = {"mode":"demo", "issues":["Dimostrazione offline con contenuti prestabiliti: nessuna revisione scientifica tramite API live."],
              "coverage":{"pages_total":2,"pages_analyzed":2,"topics_total":2,"topics_in_lessons":2,"visuals_total":1,"visuals_explained":1},
              "scope":"Dati del campione dimostrativo noto. Non è una valutazione dell'estrazione o dei modelli live."}
    output = pipeline.directory / "output"
    refs = {"D001-P0001-T01":"D001, p. 1","D001-P0002-T01":"D001, p. 2"}
    report["layout"] = await asyncio.to_thread(build_book, output, pipeline.options.title, [plan.model_dump()],
        [lesson], assets, refs, read_json(pipeline.directory / "inputs.json"), report, "demo")
    pipeline.add_layout_issues(report, report["layout"])
    await pipeline.finish(output, report, [plan], [lesson])
