# Ciclo di Carnot: prova dell'app

La [fonte del collaudo](carnot-fonte.pdf) è una scheda originale sintetica di tre pagine: grafici p-V e T-S calcolati analiticamente, tabella degli stati e cinque quesiti senza svolgimento. Non è materiale universitario caricato dall'utente.

Lo script `scripts/carnot_smoke.py` usa le normali rotte di caricamento, avvio e download dell'applicazione, con le API reali di Gemini e DeepSeek. Richiede anche `reportlab` (`python -m pip install 'reportlab>=4,<5'`).

```bash
# Prepara soltanto la scheda: nessuna API.
python scripts/carnot_smoke.py
# Generazione con API a pagamento, dopo aver configurato entrambe le chiavi.
python scripts/carnot_smoke.py --live
# Riprendi lo stesso progetto; i limiti includono il lavoro già registrato.
python scripts/carnot_smoke.py --live --resume ID --max-api-calls 60 --max-total-tokens 600000
```

L'esperimento ha permesso di correggere il contesto delle revisioni: il revisore vede un catalogo delle figure dell'intero materiale e l'autore riceve anche tabelle e dati degli altri argomenti presenti sulle pagine degli esercizi. Un errore scientifico non modifica più automaticamente i ritagli. Il renderer gestisce simboli Unicode e notazioni come `pV^γ` senza spezzare le formule. I titoli lunghi sono allineati a sinistra.

Il controllo numerico indipendente per una mole di gas ideale monoatomico, 600 K e 300 K, V_A=10 L e V_B=20 L dà V_C=56,56854 L, V_D=28,28427 L, lavoro erogato 1728,85 J e rendimento 50%. Il riferimento numerico non viene passato ai modelli.

Questa prova conserva e commenta le figure originali; non genera una sequenza di nuovi diagrammi con un ramo evidenziato alla volta. Le revisioni automatiche non certificano una preparazione infallibile per qualsiasi esame.
