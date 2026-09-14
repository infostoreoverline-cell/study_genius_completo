# Ciclo di Carnot: prova completa dell'app

La [fonte del collaudo](carnot-fonte.pdf) è una scheda originale sintetica di tre pagine: grafici p-V e T-S calcolati analiticamente, tabella degli stati e cinque quesiti senza svolgimento. Non è materiale universitario caricato dall'utente.

Lo script `scripts/carnot_smoke.py` usa le normali rotte di caricamento, avvio e download dell'applicazione, con le API reali di Gemini e DeepSeek. `reportlab` fa parte delle dipendenze del progetto e serve soltanto a ricreare la scheda sintetica.

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

Il [benchmark del preprocessing](carnot-ottimizzazione-preprocessing.json) confronta il vecchio rendering fisso con i profili adattivi: sul campione riduce i pixel del 22,54% e i byte JPEG del 36,16%, conservando grafici, etichette e tabella leggibili. Non converte automaticamente queste misure in token fatturati, perché la tokenizzazione delle immagini dipende dal provider.

## Risultato consegnato

- [Dispensa PDF](carnot-dispensa.pdf): 59 pagine, 3 capitoli e due appendici; grafici p-V e T-S commentati tratto per tratto.
- [Rapporto di qualità](carnot-qualita.md) e [dati completi JSON](carnot-qualita.json): 3/3 pagine analizzate, 7/7 argomenti coperti e 3/3 figure spiegate.
- [Sorgenti LaTeX](carnot-sorgenti.zip): documento, contenuti strutturati, evidenze e asset per l'ispezione.
- [Misure del preprocessing](carnot-ottimizzazione-preprocessing.json): benchmark locale riproducibile.

I tre capitoli hanno superato la revisione scientifica; l'ultimo ha ottenuto 98/98/98. L'audit complessivo ha ottenuto 98/98/96. Tutte le 59 pagine sono passate dai controlli locali e dalle panoramiche visive, con 11 pagine controllate anche in dettaglio. La compilazione finale non segnala fuoriuscite, glifi mancanti o pagine vuote.

Il lavoro resta correttamente marcato **da verificare**: il rapporto conserva limiti delle fonti, arrotondamenti e note di convenzione. Il collaudo ha registrato 56 chiamate, 698.990 token confermati e 2 chiamate dal consumo non confermato. Questi numeri descrivono questa esecuzione di sviluppo, non prevedono il costo di un corso diverso.

Questa prova conserva e commenta le figure originali; non genera una sequenza di nuovi diagrammi con un ramo evidenziato alla volta. Le revisioni automatiche non certificano una preparazione infallibile per qualsiasi esame.
