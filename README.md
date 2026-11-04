# Plenitude Solar Panels per Home Assistant

Integrazione custom per monitorare in Home Assistant i pannelli fotovoltaici
associati al proprio account Plenitude, tramite le API del portale My Eni.

[![Pubblicazione](https://github.com/hierro1981/EniPlenitudeSolarPanels/actions/workflows/validate.yml/badge.svg)](https://github.com/hierro1981/EniPlenitudeSolarPanels/actions/workflows/validate.yml)
[![Licenza MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Apri in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hierro1981&repository=EniPlenitudeSolarPanels&category=integration)

**Versione nel repository: `0.10.3`** · [Changelog completo](custom_components/plenitude_panels/CHANGELOG.md) · [Release pubblicate](https://github.com/hierro1981/EniPlenitudeSolarPanels/releases)

**Progetto indipendente e non ufficiale**, non affiliato a Eni o Plenitude.
Il servizio è basato sul cloud Plenitude: disponibilità, frequenza dei dati e
accesso dipendono dal portale e dalle sue API.

## Funzionalità

- Un dispositivo Home Assistant per ogni pannello restituito dal portale.
- Potenza media, energia dell'ultimo intervallo di cinque minuti e produzione
  cumulativa utilizzabile nella dashboard Energia.
- Irraggiamento quando il dato è fornito dal servizio.
- Configurazione tramite interfaccia, con traduzioni italiane e inglesi.
- Mantenimento della sessione browser ogni cinque minuti e procedura di
  riautenticazione quando il servizio rifiuta la sessione salvata.
- Recupero automatico e manuale della produzione mancante, con POD configurato.
- Analisi, riparazione e annullamento dell'ultima riparazione delle statistiche.

## Requisiti

- Home Assistant con supporto alle integrazioni custom e accesso a Internet.
- Account Plenitude con pannelli visibili nella relativa sezione del portale.
- Una sessione browser autenticata da cui ricavare Cookie, User-Agent,
  account ID e supply ID.
- Facoltativo: POD e Recorder attivo per le funzionalità storiche descritte
  nella [guida al recupero](docs/recupero-produzione.md).
- HACS già configurato, se si sceglie l'installazione tramite HACS.

Non servono Node.js, npm, Playwright o modifiche a `configuration.yaml`.
I test coprono Home Assistant **2025.10.4** con Python **3.13** e
**2026.9.4** con Python **3.14**; non dichiarano la compatibilità con tutte
le altre versioni. L'integrazione richiede Babel **2.18.0 o successivo**: su versioni precedenti
di Home Assistant un'altra integrazione potrebbe richiedere una versione diversa
della stessa libreria; in caso di conflitto aggiorna Home Assistant.
Le icone locali dell'integrazione sono supportate da Home Assistant 2026.3.

## Installazione

### Tramite HACS

Finché il repository non sarà incluso nel catalogo predefinito, aggiungilo
come **repository personalizzato**:

1. Apri HACS in Home Assistant.
2. Dal menu seleziona **Repository personalizzati**.
3. Inserisci `https://github.com/hierro1981/EniPlenitudeSolarPanels` e scegli
   il tipo **Integrazione**.
4. Cerca **Plenitude Solar Panels**, aprilo e scaricalo.
5. Riavvia Home Assistant.
6. Vai in **Impostazioni > Dispositivi e servizi > Aggiungi integrazione**
   e cerca **Plenitude Solar Panels**.

Il pulsante **Apri in HACS** in cima alla pagina è una scorciatoia per aprire
il repository nella propria istanza. I nomi dei menu possono variare tra
versioni HACS/Marketplace.

### Installazione manuale

1. Scarica il repository dalla release desiderata o dal ramo predefinito.
2. Copia la cartella `custom_components/plenitude_panels/` nella cartella
   `custom_components/` della configurazione Home Assistant.
3. Verifica che il percorso finale sia:

   ```text
   <config>/custom_components/plenitude_panels/manifest.json
   ```

4. Riavvia Home Assistant e aggiungi l'integrazione da **Dispositivi e servizi**.

`<config>` è la cartella contenente `configuration.yaml`, spesso `/config`.
Non copiare l'intero repository in `custom_components`.

## Configurazione

### Recuperare i dati dal browser

Il login iniziale richiede il browser perché il portale può usare CAPTCHA e OTP.

1. Accedi a [My Eni](https://eniplenitude.com/my-eni) e apri la sezione dei pannelli.
2. Apri gli strumenti sviluppatore del browser (**F12**) e la scheda
   **Rete / Network**. Ricarica la pagina per acquisire le richieste.
3. Filtra per `pannelli-services` e seleziona una richiesta riuscita, per esempio:

   ```text
   /serviceDAp/api/pannelli-services/v1/accounts/<ACCOUNT_ID>/supply/<SUPPLY_ID>/details
   ```

4. Dall'URL ricava `ACCOUNT_ID` e `SUPPLY_ID`; dagli **header della richiesta**
   copia i valori completi di `Cookie` e `User-Agent`, dalla **stessa richiesta**.
   Per Cookie copia solo il valore, senza il prefisso `Cookie:`; deve comprendere
   `GYSESSION`, `GAESA` e gli altri cookie della sessione.
5. Per il recupero storico, ricava anche il POD della fornitura, quello usato
   dalla richiesta `productions-consumes`. **POD e supply ID sono distinti**.

### Campi dell'integrazione

| Campo | Descrizione |
| --- | --- |
| Cookie | Header completo della sessione autenticata. |
| User-Agent | Valore completo copiato dalla stessa richiesta del Cookie. |
| Account ID | Identificativo presente nel percorso API dopo `accounts/`. |
| Supply ID | Identificativo presente nel percorso API dopo `supply/`. |
| POD | Facoltativo per il monitoraggio; necessario per il recupero storico. |
| Recupera produzione mancante | Abilitato per impostazione predefinita; attende un POD prima di operare. |
| Intervallo aggiornamento | Minimo e valore consigliato: **5 minuti**. |

I dati vengono verificati prima del salvataggio. Per modificarli successivamente,
apri l'integrazione e scegli **Configura > Impostazioni**.

La sessione viene mantenuta all'avvio e ogni cinque minuti, anche con intervalli
di lettura dei pannelli più lunghi. Questo non garantisce che il portale la
mantenga valida indefinitamente. Quando viene richiesta una riautenticazione,
effettua un nuovo login nel browser e aggiorna insieme Cookie e User-Agent.

Cookie e configurazione vengono salvati da Home Assistant in `.storage`, senza
cifratura applicativa. Non pubblicarli nelle issue o nei log condivisi.

## Sensori e dashboard Energia

I nomi delle entità attualmente esposti sono:

| Sensore | Unità | Significato |
| --- | --- | --- |
| `Average Power (5 min)` | W | Potenza media dell'intervallo di cinque minuti riportato dall'API. |
| `Production (5 min)` | Wh | Energia dello stesso intervallo. |
| `Total Production` | Wh | Energia cumulativa, ripristinata dopo i normali riavvii. |
| `Irradiance` | W/m² | Irraggiamento, se disponibile nell'API. |

Il valore live dell'API è equivalente a una potenza media: l'integrazione lo
mantiene per il sensore di potenza e lo divide per 12 per ottenere l'energia
dei cinque minuti. I valori aggregati storici sono già energia d'intervallo.

Per la dashboard Energia:

1. Apri **Impostazioni > Dashboard > Energia**.
2. Nella sezione dedicata alla produzione solare, aggiungi una sorgente.
3. Seleziona il sensore **Total Production** del pannello. Ripeti per gli altri.

`Total Production` ha classe `energy` e stato `total_increasing`. Il totale
parte dalla prima lettura utile; non è il contatore storico del portale.
Mantieni le letture ogni **5 minuti**: intervalli maggiori possono perdere
finestre di produzione. Le finestre offline non vengono integrate automaticamente
come se fossero state misurate; il recupero storico ha requisiti specifici.
Recorder deve registrare i sensori per produrre le statistiche della dashboard.

## Recupero e riparazione della produzione

Con un POD configurato, l'integrazione può confrontare i campioni locali con
la produzione aggregata del servizio e ricostruire alcuni dati mancanti.
La ripartizione per pannello può essere **stimata**: l'API aggregata non permette
sempre di distinguere un guasto reale da telemetria assente.

Dal menu **Configura** sono disponibili recupero manuale, riparazione delle
statistiche e annullamento dell'ultima riparazione. Le procedure mostrano
un'anteprima e richiedono conferma.

Consulta la [guida al recupero e alle statistiche](docs/recupero-produzione.md)
prima di usare queste funzioni.

## Aggiornamenti

- **HACS:** scarica l'aggiornamento proposto e riavvia Home Assistant.
- **Manuale:** sostituisci `custom_components/plenitude_panels/` con la nuova
  versione e riavvia Home Assistant.

Non è necessario eliminare e ricreare l'integrazione per questo riordino del
repository. Consulta il [changelog](custom_components/plenitude_panels/CHANGELOG.md)
per le modifiche di ogni versione.

## Risoluzione dei problemi

| Problema | Verifica consigliata |
| --- | --- |
| L'integrazione non compare | Controlla il percorso di `manifest.json`, riavvia Home Assistant e verifica i log. |
| Sessione non valida, HTTP 401/403 | Accedi nuovamente al portale e aggiorna Cookie e User-Agent insieme. |
| HTTP 406 | La richiesta è stata rifiutata: non prova da sola la scadenza del Cookie. Controlla gli header della richiesta riuscita; le protezioni del sito possono considerare anche rete e altre caratteristiche del client. |
| Irraggiamento non disponibile | Il servizio potrebbe non restituire quel dato. |
| Recupero in attesa | Controlla POD, disponibilità della giornata aggregata e completezza dei campioni; vedi la guida. |
| Produzione negativa nella dashboard | Usa l'analisi di **Ripara statistiche** per il giorno interessato, come descritto nella guida. |

Per segnalare un problema apri una
[issue](https://github.com/hierro1981/EniPlenitudeSolarPanels/issues) indicando
versione di Home Assistant, versione dell'integrazione, comportamento atteso e
passaggi per riprodurlo. Allega solo log ripuliti da credenziali e dati personali.

## Documentazione e sviluppo

- [Recupero produzione e riparazione statistiche](docs/recupero-produzione.md)
- [Sviluppo e test](docs/sviluppo.md)
- [Pubblicazione e inclusione nel catalogo HACS](docs/pubblicazione-hacs.md)
- [Riferimento tecnico dettagliato, in inglese](docs/technical-reference.md)
- [Strumenti Node opzionali e cancellazione manuale](tools/node/README.md)

```text
custom_components/plenitude_panels/  Integrazione installata da HACS/Home Assistant
tests/                             Test e fixture
docs/                              Guide e riferimento tecnico
tools/node/                        Strumenti diagnostici opzionali
.github/workflows/                 Controlli e pubblicazione manuale delle release
```

## Licenza

Distribuito con [licenza MIT](LICENSE). Nomi e marchi citati appartengono
ai rispettivi titolari. L'icona solare inclusa è un disegno originale del progetto.
