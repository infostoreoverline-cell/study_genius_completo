# ADR-002 · Profili editoriali e controllo della deriva

- **Stato:** accettato
- **Versione:** StudyGenius 1.5
- **Decisione:** 16 settembre 2026

## Problema osservato

Nel caso reale `Catalisi A-CI-2024.pdf`, la fonte contiene 20 pagine e circa 1.899 parole
estraibili. La modalità presentata come più corta ha prodotto `prova pdf.pdf`: 152 pagine e
circa 48.974 parole. L'espansione è quindi 7,6 volte in pagine e quasi 26 volte in parole.

La causa non era soltanto il modello. Il contratto imponeva a ogni capitolo almeno un
esercizio, tre domande di richiamo, una mappa con più topic, una sintesi e un'appendice con
tutte le risposte. Inoltre il planner poteva creare molti capitoli e la validazione verificava
la copertura, ma non la ripetizione dello stesso topic tra sezioni né la lunghezza totale.

## Decisione

L'evidenza scientifica estratta dalle pagine resta lo strato canonico completo. La forma
pubblicata è una proiezione controllata di quello strato secondo uno dei tre profili:

| Profilo | Scopo | Comportamento distintivo |
| --- | --- | --- |
| `summary` | Riassunto breve | Nessun esercizio inventato, nessun richiamo duplicato in appendice, solo visuali essenziali |
| `study` | Dispensa ragionata | Spiegazione autosufficiente, esercizi e richiamo attivo mirati |
| `transcript` | Sbobina estesa | Più passaggi ed esempi, sempre senza duplicazioni concettuali |

Il budget deriva dal numero di pagine e dalla densità testuale. Le slide possono ricevere lo
spazio necessario per spiegare formule implicite; un libro denso viene invece compresso in
modo più aggressivo. Per Catalisi, il profilo breve assegna circa 5.356 parole e un massimo
rigido di circa 6.963, non 49.000.

## Garanzie applicative

1. Titoli identici o fortemente sovrapposti vengono riuniti in unità concettuali prima del
   planner, conservando tutti i riferimenti originali.
2. Il planner riceve lo scopo editoriale in linguaggio naturale e un numero obiettivo di
   capitoli; un indice troppo frammentato non supera la validazione.
3. Ogni `topic_id` deve apparire esattamente una volta nelle sezioni del libro.
4. Ogni capitolo ha limiti di parole, sezioni, paragrafi, esercizi, richiami, mappe e grafici.
5. La somma delle lezioni deve rientrare nel limite globale prima della composizione PDF.
6. Il revisore riceve lo stesso mandato editoriale e non può trasformare una sintesi
   intenzionale in una richiesta automatica di espansione.
7. Il rapporto qualità registra il budget e il rapporto di espansione per rendere verificabile
   il comportamento su fonti piccole e libri di centinaia di pagine.

## Effetti su velocità e affidabilità

Meno capitoli significano meno stesure DeepSeek, meno revisioni Gemini, meno anteprime LaTeX
e meno asset da controllare. I limiti API, la validazione Pydantic, i checkpoint, la review
scientifica e la tracciabilità delle fonti restano invariati. Una risposta fuori budget viene
rifiutata localmente: il vincolo non dipende dall'obbedienza del modello.

## Limiti

Il numero finale di pagine non è perfettamente prevedibile, perché formule, tabelle e figure
occupano spazio diverso dalla prosa. Per questo la garanzia primaria è sul contenuto validato,
mentre il controllo di impaginazione continua a misurare il PDF compilato. La deduplicazione
lessicale è conservativa; il planner e il revisore restano responsabili delle equivalenze
scientifiche non riconoscibili dal solo titolo.
