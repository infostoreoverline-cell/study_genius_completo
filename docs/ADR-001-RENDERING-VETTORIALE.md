# ADR-001 — Rendering vettoriale nativo

- **Stato:** accettato
- **Versione:** StudyGenius 1.4
- **Ambito:** grafici e mappe concettuali estratti dalle fonti

## Decisione

Le figure pubblicate nella dispensa non sono più ritagli raster ottenuti da coordinate
`bbox`. Gemini interpreta la pagina completa e consegna esclusivamente dati semantici
validati:

- `SchedaAnaliticaGrafico` per assi, curve, formule, parametri e punti critici;
- `SchedaMappa` per nodi, relazioni e percorso di lettura.

Matplotlib genera i grafici e Graphviz (`dot`) genera le mappe. Il file inserito nel
LaTeX è sempre PDF vettoriale. Una preview PNG viene generata soltanto per la review
multimodale e non viene pubblicata come figura finale.

## Confine di affidabilità

PyMuPDF rimane necessario per:

- rendere la pagina completa che Gemini deve leggere;
- estrarre testo;
- controllare geometricamente ogni pagina del PDF finale;
- creare le tavole di revisione finale.

ADR-001 elimina quindi il *crop bitmap delle figure*, non l'acquisizione delle pagine.
Fotografie, micrografie, strutture molecolari e illustrazioni non ricostruibili non vengono
approssimate: restano descritte nelle evidenze e accompagnate da un'incertezza verificabile.

## Sicurezza

Il modello non produce SVG, DOT, TeX o codice eseguibile. Il DOT viene composto localmente
da ID, nodi e archi Pydantic; `dot` è invocato senza shell e con timeout. Le formule
della scheda attraversano la stessa whitelist matematica del documento. I caratteri
speciali hanno escape separati per DOT e LaTeX.

## Impaginazione

Ogni figura sorgente usa:

```latex
\begin{figure}[htbp]
\centering
\includegraphics[width=0.85\linewidth]{grafico_vettoriale.pdf}
\end{figure}
```

Segue un `tcolorbox` con significato fisico/chimico, assi, formule, parametri, punti
chiave, passaggi analitici e limiti dichiarati.
