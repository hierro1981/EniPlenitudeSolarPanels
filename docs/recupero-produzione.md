# Recupero produzione e riparazione statistiche

[← README](../README.md)

Queste funzioni richiedono un **POD** valido e dati storici pubblicati dall'API
`productions-consumes`. Recorder deve includere i sensori `Total Production`
per le correzioni storiche. Il fuso orario di Home Assistant determina i giorni.

## Recupero automatico

Con **Recupera produzione mancante** abilitato, l'integrazione conserva sette
giorni di campioni locali attraverso i riavvii e controlla le giornate concluse
una volta all'ora. Ogni lettura live è attribuita ai cinque minuti precedenti,
anche quando ricadono nel giorno precedente a mezzanotte.

La procedura confronta i campioni con la produzione aggregata a quarti d'ora.
Per pannelli senza letture positive nell'intera giornata sottrae la produzione
registrata dagli altri pannelli e ripartisce il residuo positivo in proporzione
alla potenza nominale. Se una potenza necessaria manca o vale zero, usa quote
uguali. I pannelli con letture positive nella giornata non vengono ricostruiti.

È una **stima**, non una misura individuale: il dato aggregato non distingue
necessariamente un pannello fermo da un pannello con telemetria mancante.

Servono tutti e tre i campioni di ogni quarto d'ora produttivo, dati validi
sugli altri pannelli, inventario stabile e giornata aggregata completa.
Intervalli mancanti o letture più lente di cinque minuti rinviano il recupero.
Risposte aggregate incomplete o tutte a zero vengono ritentate nella finestra
dei sette giorni. I timestamp ambigui nel cambio all'ora solare vengono rinviati;
le giornate da 23/25 ore sono supportate se i timestamp identificano l'offset.

## Recupero manuale

Apri **Impostazioni > Dispositivi e servizi > Plenitude Solar Panels >
Configura > Recupera produzione**. È disponibile anche con recupero automatico
disabilitato e non ha il limite di sette giorni, purché l'API restituisca i dati.

1. Scegli una data precedente a oggi.
2. L'integrazione analizza i campioni locali, integrando le lacune con gli stati
   cumulativi originali di Recorder quando utilizzabili. Non riempie i buchi
   inventando letture.
3. Se i campioni bastano, propone i residui per intervallo sui pannelli senza
   produzione positiva. Altrimenti richiede destinatari e **produzione originale
   già conteggiata su tutti i pannelli, esclusi i recuperi precedenti**, in Wh.
   Se non è stato conteggiato nulla, inserisci esplicitamente `0`.
4. Controlla aggregato, energia originale, recuperi precedenti e nuove quote
   per pannello. Puoi modificare l'assegnazione prima di confermare.
5. Conferma l'applicazione. Chiudere prima della conferma non assegna energia.

La modalità **≈** usa una stima giornaliera distribuita sul profilo aggregato;
**—** indica dato non disponibile e **≥** un limite inferiore da campioni parziali.
Lo zero proposto quando tutti i pannelli sono senza dati è un'ipotesi modificabile,
non una misura. Un totale inserito manualmente non viene inventato come produzione
originale dei singoli pannelli.

La schermata finale consente di passare al giorno precedente o successivo:
ogni nuova giornata richiede analisi e conferma. Il registro persistente dei
recuperi evita di aggiungere nuovamente quote già assegnate. Un'anteprima non
più valida richiede una nuova analisi.

## Effetti sui sensori e sulla dashboard Energia

L'energia recuperata aumenta `Total Production` dopo il salvataggio persistente.
Dopo che Recorder ha registrato e compilato l'aumento, la correzione sposta il
contributo statistico sulla giornata originale. L'aggiornamento della dashboard
può quindi richiedere ulteriore tempo e un successivo ciclo di cinque minuti.
La cronologia degli stati grezzi continua a mostrare quando il totale è cambiato.

Gli attributi del sensore espongono `live_energy_wh`, `recovered_energy_wh`, `last_recovered_day`,
`recovery_status` e `recovery_pending_statistics`. I marcatori
`plenitude_panels:recovery_audit_*` servono a rendere i tentativi ripetibili;
non sono sorgenti da selezionare nella dashboard Energia.

Disabilitare il recupero automatico ferma nuove assegnazioni automatiche,
conserva quelle già applicate e consente di completarne le correzioni statistiche.
Le correzioni rifiutano di attraversare somme cumulative decrescenti: in quel
caso occorre prima riparare la giornata anomala.

## Ripara statistiche

Per picchi positivi o negativi nella dashboard, scegli **Configura > Ripara statistiche**:

1. Seleziona una giornata conclusa.
2. Esamina il confronto tra aggregato API e statistiche orarie di Recorder.
3. Seleziona i pannelli interessati e confronta i totali correnti con i proposti.
4. Conferma solo dopo aver controllato l'anteprima.

La selezione suggerisce anche i pannelli il cui totale positivo supera l'intero
aggregato API del giorno. Controlla comunque tutti i pannelli: il suggerimento
non identifica ogni possibile anomalia.

La riparazione **sostituisce le curve statistiche giornaliere** dei pannelli
selezionati con un profilo stimato dall'aggregato, ripartito per potenza nominale.
Conserva i totali validi dei pannelli non selezionati e considera i recuperi
precedenti dei selezionati. Sposta le somme successive per preservarne gli
incrementi. Non cambia totali live, energia recuperata o cronologia degli stati
grezzi. Anomalie indipendenti in altre giornate richiedono interventi separati.

Servono statistiche per tutti i pannelli e recuperi pendenti già registrati e
compilati; recuperi pendenti di altre date devono essere risolti. I confini del
giorno locale devono allinearsi alle ore UTC; timestamp ambigui sono rinviati.
Campioni aggregati negativi finiti sono trattati come zero e conteggiati
nell'analisi, mentre valori assenti o invalidi restano indisponibili.

### Riavvio dopo una riparazione

Dalla versione **0.11.2**, `Total Production` salva un checkpoint nativo con quota
live, riferimento al recupero e ultimo campione conteggiato, anche se lo stato
pubblicato è `unavailable`. La riparazione e il suo annullamento non modificano
questo checkpoint. Per i salvataggi precedenti senza checkpoint viene usato lo
stato numerico ripristinato oppure, se indisponibile, l'ultimo stato grezzo valido
di Recorder. Le somme statistiche corrette non vengono usate come contatore live.

Se esiste un vecchio contatore ma non è disponibile una base coerente (oppure il
registro dei recuperi è inferiore al riferimento salvato), il sensore resta
indisponibile e segnala il problema nei log, senza pubblicare un falso reset.
Ripristina i dati salvati coerenti e ricarica l'integrazione. I nuovi pannelli
senza alcuno stato precedente iniziano normalmente da zero.

Un precedente calo del cumulativo può avere già generato un picco positivo o una
produzione negativa nel dashboard Energia: Home Assistant può interpretare un
calo superiore al 10% come un reset, mentre uno più piccolo può generare una
variazione negativa. L'aggiornamento evita la perdita live al ripristino ma non
riscrive automaticamente anomalie già registrate né cerca massimi storici da
reinserire nel totale corrente.

Per correggere una giornata già contaminata, installa prima la versione corretta,
salva un backup comprensivo di Recorder e ripara **tutti i pannelli anomali** di
quel giorno. Se sono tutti selezionati, la somma dei proposti deve corrispondere
all'aggregato API; conferma dopo aver verificato l'anteprima. Attendi la successiva
compilazione delle statistiche e verifica la continuità anche dopo un riavvio.

## Annullare l'ultima riparazione

**Configura > Annulla ultima riparazione statistiche** ripristina i valori salvati
prima dell'ultima riparazione, se righe e marcatori non sono stati modificati
nel frattempo. Le statistiche aggiunte successivamente sono adeguate per togliere
l'offset della riparazione. Una nuova riparazione sostituisce il punto di ripristino.
Il journal persistente riconcilia operazioni interrotte al successivo heartbeat
Recorder o alla riapertura dello strumento.

Per dettagli su retention, migrazione dei registri precedenti e transazioni,
consulta il [riferimento tecnico](technical-reference.md).
## Protezione delle nuove letture anomale

Il limite per pannello descritto nel [README](../README.md#limite-di-produzione-per-pannello)
viene applicato prima di salvare i campioni live usati dal recupero. I campioni
superiori alla soglia attiva vengono limitati, non redistribuiti nel tempo.
Il controllo non corregge campioni già salvati e non limita le allocazioni
ricostruite dai dati aggregati storici.
