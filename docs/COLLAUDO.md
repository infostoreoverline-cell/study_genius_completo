# Collaudo del 12 settembre 2026

## Risultato

L'applicazione è stata eseguita su Linux con Python 3.12 e XeLaTeX. Il collaudo comprende test automatici, un browser Chromium reale e una generazione a pagamento con **Gemini e DeepSeek reali**.

| Verifica | Risultato |
| --- | --- |
| Suite automatica | 32 test superati |
| Pipeline di integrazione | PDF realmente compilato; risposte API simulate; pausa e ripresa verificate |
| Interfaccia | Desktop 1440 px, mobile 390 px, impostazioni, demo, download PDF e ritorno ai progetti |
| Demo | Nessuna richiesta API; PDF selezionabile prodotto da XeLaTeX |
| Generazione live | `gemini-3.8-flash` + `deepseek-v4-pro` |
| Fonte live | PDF sintetico di 2 pagine sui gas ideali, 3 argomenti identificati, 1 grafico originale |
| Dispensa live | 2 capitoli, 20 pagine, teoria, derivazioni, esercizi e richiamo attivo |
| Copertura tracciata | 2/2 pagine, 3/3 argomenti identificati, 1/1 figure spiegate |
| Compilazione finale | Nessun elemento oltre i margini sopra 2 pt, nessun glifo mancante, nessuna pagina vuota |
| Confronto finale | Le immagini delle 20 pagine ricompilate coincidono con quelle sottoposte alla revisione visiva |

File: [dispensa live](../examples/collaudo-live.pdf), [fonte usata](../examples/fonte-gas-ideale.pdf), [sorgenti e verifiche](../examples/collaudo-live-sorgenti.zip), [rapporto JSON](../examples/collaudo-live-qualita.json), [demo senza API](../examples/demo-gas-ideale.pdf).

## Cosa ha verificato la prova live

Gemini ha letto testo e immagine di entrambe le pagine. DeepSeek ha costruito l'indice e scritto i capitoli; Gemini ha confrontato le spiegazioni con le fonti. Sono state verificate la legge del gas ideale, la curva isoterma, le conversioni tra kPa/L/J e la convenzione chimica del lavoro. Nell'esempio una mole a 300 K passa da 10 a 20 L: il lavoro ricevuto è circa −1,73 kJ, il calore assorbito circa +1,73 kJ.

Prima della scrittura viene ora condivisa una guida alle convenzioni: la prima versione di prova aveva mostrato che capitoli singolarmente comprensibili possono adottare convenzioni differenti. L'indice completo e la guida comune riducono anche le ripetizioni tra capitoli.

La prova ha inoltre permesso di correggere:

- Il rifiuto di alcuni schemi JSON complessi da parte dell'endpoint Gemini: ripiego limitato sulla modalità JSON e validazione locale integrale.
- Le formule con uno strato di escape JSON in eccesso, che altrimenti potevano compilare con una resa errata.
- La notazione matematica riconoscibile lasciata senza delimitatori nella prosa e le formule nei titoli.
- Titoli isolati dalle formule o figure successive, prefissi numerici duplicati nelle liste e l'impronta del documento oltre i margini.
- L'accesso a nuovi progetti e alla cronologia su schermi stretti.

## Rilievi residui e significato del risultato

I due capitoli hanno superato la revisione automatica. I punteggi nel JSON sono valutazioni del modello, **non percentuali di accuratezza misurate**. Il documento resta marcato **da verificare** perché conserva i limiti dichiarati dalle fonti: arrotondamenti, assenza di incertezze quantitative e ambito di validità del modello ideale.

La revisione visiva segnala anche rilievi tipografici minori in alcune formule: unità in corsivo e spaziature migliorabili, oltre alla sillabazione di alcuni titoli. Sono riportati integralmente nel JSON; non sono presentati come difetti risolti. Non sono emersi tagli, sovrapposizioni o glifi mancanti nella versione consegnata.

Il registro locale del progetto riporta **34 tentativi**, **233.248 token confermati** e **3 richieste con consumo non confermato**. Include sviluppo, riprese e più verifiche dell'impaginazione: non è una stima del consumo di una singola esecuzione nuova. Sono state effettuate anche piccole richieste diagnostiche separate. Il costo effettivo va verificato nei pannelli dei provider.

## Ambito non collaudato

- Installazione su Windows/MiKTeX, macOS e Docker: procedure e file forniti; non eseguiti su quei sistemi in questa sessione.
- Raccolte vicine al limite di 1.500 pagine, scansioni molto degradate e materie diverse dal campione: nessuna prova estesa su questi casi.
- Il test su due pagine verifica il flusso e fa emergere problemi concreti; non dimostra una preparazione infallibile per qualsiasi esame.

I test automatici sono ripetibili senza chiavi. Lo script `scripts/live_smoke.py --live` richiede entrambe le chiavi e utilizza credito API. Le chiavi usate nella sessione non sono incluse nella repository né negli esempi.
