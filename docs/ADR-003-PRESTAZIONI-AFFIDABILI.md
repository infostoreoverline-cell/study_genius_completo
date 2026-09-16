# ADR-003 · Prestazioni senza ridurre i controlli

- **Stato:** accettato
- **Versione:** StudyGenius 1.6

## Contesto

Su dispense con decine di slide, il rendering iniziale del PDF richiede pochi secondi; la
latenza percepita deriva soprattutto dalle richieste ai provider e dalle compilazioni LaTeX
ripetute durante i cicli autore-revisore. Alzare indiscriminatamente la concorrenza o saltare
le revisioni ridurrebbe il tempo, ma renderebbe meno prevedibili quota, affidabilità e qualità.

## Decisione

1. L'indice e la guida delle convenzioni partono insieme perché usano provider diversi e
   dipendono dallo stesso strato di evidenze. I semafori esistenti restano autoritativi.
2. Se una delle due operazioni fallisce, l'altra viene cancellata e attesa prima di propagare
   l'errore originale; non restano richieste in background dopo la chiusura del lavoro.
3. Ogni bozza viene ancora compilata con il motore TeX reale e con le figure reali, ma
   l'anteprima omette il boilerplate deterministico e usa un passaggio. La consegna finale
   conserva la convergenza multipass, il controllo locale di tutte le pagine e la review
   visiva gerarchica.
4. Le piccole immagini decorative non promuovono da sole una pagina al profilo tecnico. Le
   formule, il testo minuto, le immagini significative e i disegni vettoriali continuano a
   determinare la risoluzione; ogni dubbio esplicito del lettore attiva il fallback ad alta
   definizione.
5. Il rapporto di qualità registra tempi per fase e per capitolo. Le misure contengono solo
   durate e conteggi, mai chiavi, prompt o risposte.

## Conseguenze

La pipeline elimina attese seriali e compilazioni ridondanti senza rimuovere controlli
scientifici o tecnici. Il guadagno effettivo resta dipendente da provider, quota e struttura
del documento; per questo viene misurato per ogni progetto invece di essere promesso come
percentuale universale.
