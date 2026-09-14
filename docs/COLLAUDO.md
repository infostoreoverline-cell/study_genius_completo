# Collaudo del 14 settembre 2026

## Risultato

StudyGenius 1.1 è stato eseguito su Linux con Python 3.12, XeLaTeX e le API reali di Gemini e DeepSeek. Il caso principale parte da una scheda sintetica sul ciclo di Carnot che contiene grafici analitici p-V e T-S, una tabella degli stati e cinque quesiti senza svolgimento. La pipeline ha prodotto una dispensa selezionabile di 59 pagine.

| Verifica | Risultato |
| --- | --- |
| Suite automatica | 48 test superati |
| Pipeline di integrazione | PDF realmente compilato; risposte API simulate; limiti, pausa e ripresa verificati |
| Fonte Carnot | 3 pagine, 7 argomenti identificati, 3 figure, 5 quesiti |
| Dispensa live | 3 capitoli, 2 appendici, 59 pagine |
| Copertura tracciata | 3/3 pagine, 7/7 argomenti identificati, 3/3 figure spiegate |
| Revisione dei capitoli | Tutti superati; punteggi finali 95/96/95, 93/96/92 e 98/98/98 |
| Audit complessivo | Superato: copertura 98, correttezza 98, chiarezza 96 |
| Quesiti della fonte | Un esercizio sorgente con 5 punti richiesti e 25 passaggi di soluzione |
| Controllo visivo | Tutte le pagine in panoramiche numerate; 11 pagine a rischio anche a piena risoluzione |
| Compilazione finale | Nessuna fuoriuscita rilevata, nessun glifo mancante, nessuna pagina vuota |
| Stato consegnato | `needs_review`, per caveat dichiarati nel rapporto e non per fallimenti scientifici o di compilazione |

File: [dispensa Carnot](../examples/carnot/carnot-dispensa.pdf), [fonte usata](../examples/carnot/carnot-fonte.pdf), [sorgenti e verifiche](../examples/carnot/carnot-sorgenti.zip), [rapporto JSON](../examples/carnot/carnot-qualita.json), [rapporto leggibile](../examples/carnot/carnot-qualita.md), [benchmark](../examples/carnot/carnot-ottimizzazione-preprocessing.json).

## Cosa dimostra sul ciclo di Carnot

La dispensa non si limita a descrivere l'immagine. Mantiene gli stati A, B, C e D della fonte e scompone entrambi i diagrammi nei quattro rami. Per ciascun ramo collega trasformazione, direzione, cilindro-pistone, sorgente termica, grandezze costanti, scambi energetici, equazione e forma della curva.

La parte quantitativa ricava le pendenze nello stesso stato, calcola gli stati non letti dal grafico e chiude i bilanci. Un riferimento numerico indipendente, non passato ai modelli, dà:

- \(V_C=56{,}568542\,\mathrm{L}\) e \(V_D=28{,}284271\,\mathrm{L}\);
- \(\Delta S_{AB}=5{,}762826\,\mathrm{J/K}\);
- lavoro netto erogato \(W_{out}=1728{,}85\,\mathrm{J}\);
- rendimento \(\eta=0{,}500\).

Gli stessi valori compaiono nella soluzione finale. La convenzione chimica \(\Delta U=q+w\) è tenuta distinta dal lavoro erogato \(W_{out}=-w\). L'appendice A contiene risposte motivate alle domande di richiamo; per questo il rilievo dell'audit sulla mancanza di un titolo d'appendice è un falso positivo del contesto di audit della versione eseguita. Il codice aggiornato passa ora all'audit anche l'elenco delle appendici renderizzate.

## Verifica dell'impaginazione

La compilazione avviene in una directory pulita e univoca. XeLaTeX esegue fino a quattro passaggi e si ferma quando i file ausiliari convergono; il candidato viene aperto con PyMuPDF e sostituisce il PDF pubblico in modo atomico. Questo ha risolto due difetti osservati durante il collaudo: un file ausiliario corrotto lasciato da un processo interrotto e l'indice non ancora stabilizzato dopo due passaggi.

Il PDF finale ha 59 pagine A4, testo selezionabile e segnalibri per capitoli, sezioni e appendici. Un controllo geometrico indipendente non ha trovato testo oltre i margini né blocchi lunghi sotto 7 pt. Le formule numeriche più dense sono state ispezionate a piena risoluzione e restano leggibili.

Le sette revisioni visive Gemini hanno superato il gate. Una di esse aveva correttamente segnalato l'indice sfasato prima dell'ultimo passaggio locale; dopo la ricompilazione convergente i numeri sono stati verificati nel testo estratto: “Prima di iniziare” 3, capitoli 4/21/37 e appendici 56/58. Non viene attribuita a Gemini una nuova revisione che non è stata eseguita.

## Prestazioni e costi

Il benchmark riproducibile confronta il vecchio rendering fisso a 150 DPI con il preprocessing adattivo sulla stessa fonte:

| Metrica | Prima | Dopo | Variazione |
| --- | ---: | ---: | ---: |
| Pixel inviabili | 6.530.142 | 5.058.144 | −22,54% |
| Byte JPEG | 594.486 | 379.528 | −36,16% |
| Schema `Lesson` serializzato | 5.647 caratteri | 5.103 caratteri | −9,63% |
| Chiamate previste per review visiva | 15 | 7 | −53,33% |

Le riduzioni di pixel e byte non equivalgono automaticamente a token o euro risparmiati: ogni provider applica una propria tokenizzazione delle immagini. La qualità resta protetta da una rilettura mirata a 190 DPI quando il lettore dichiara un dubbio di leggibilità.

La singola esecuzione Carnot conservata nel rapporto registra **56 chiamate**, **336.130 token di input**, **362.860 token di output** e **698.990 token totali confermati**. Di questi, 512 token di input sono dichiarati come serviti dalla cache; 2 chiamate hanno consumo non confermato. Il costo effettivo va verificato nei pannelli dei provider. Questi dati includono retry e revisioni di sviluppo e non sono una previsione per un'altra raccolta di PDF.

## Perché il risultato resta “da verificare”

Il rapporto conserva dieci note: valori tabellari arrotondati, coordinate intermedie non presenti nella fonte, entropia soltanto relativa, riferimenti bibliografici generali e distinzione tra due convenzioni di lavoro. Una nota dell'audit sull'appendice è un falso positivo, ma viene lasciata nel rapporto automatico per non riscrivere retroattivamente il responso del modello.

I punteggi delle revisioni sono giudizi dei modelli, non percentuali di accuratezza misurate. La fonte è un campione sintetico e non un intero corso universitario. Il collaudo mostra che il sistema sa leggere, scomporre, calcolare, spiegare e impaginare questo caso; non certifica una preparazione infallibile per qualsiasi esame.

## Ambito non collaudato

- Installazione su Windows/MiKTeX, macOS e Docker: procedure e file sono forniti, ma non sono stati eseguiti in questa sessione.
- Raccolte vicine al limite di 1.500 pagine, scansioni molto degradate e materie diverse dal campione.
- Accuratezza bibliografica autonoma: StudyGenius non effettua ricerca esterna né sostituisce programma, docente o manuali ufficiali.

I test automatici non richiedono chiavi. `scripts/carnot_smoke.py --live` utilizza credito API. Le chiavi usate durante il collaudo non sono incluse nella repository, negli archivi o nei rapporti.
