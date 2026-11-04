# Strumenti Node.js opzionali — candidati alla cancellazione manuale

Questa cartella contiene i prototipi e gli strumenti di diagnosi delle API
Plenitude. **Home Assistant non li esegue e non richiede Node.js, npm o
Playwright.** L'intera cartella `tools/node/` può essere cancellata manualmente
se non serve più per lo sviluppo. Non cancellare file della configurazione di
Home Assistant: questi strumenti sono indipendenti dall'integrazione.

| File | Scopo residuo |
| --- | --- |
| `call-details.mjs` | Diagnosi dell'inventario pannelli. |
| `call-instant-production.mjs` | Diagnosi della produzione istantanea. |
| `call-benefits.mjs` | Esplorazione dell'API benefici, non esposta dall'integrazione. |
| `refresh-session.mjs`, `session-refresh.mjs` | Prototipo del refresh sessione, già implementato autonomamente in Python. |
| `login.mjs` | Ausilio sperimentale al login con browser visibile. |
| `monitor-session.mjs` | Analisi della durata della sessione browser. |
| `package.json`, `package-lock.json`, `.env.example` | Supporto ai soli strumenti di questa cartella. |

Tutti i comandi seguenti vanno eseguiti **dalla cartella `tools/node/`**.
Eventuali vecchi `.env`, `node_modules/` e `monitor-logs/` locali nella radice
non vengono spostati automaticamente: verifica se conservarli o rimuoverli.

## Uso degli strumenti

Richiede Node.js 22 o successivo. Gli script di chiamata API usano le API native
di Node. Per `login.mjs` e `monitor-session.mjs` installa anche Playwright:

```sh
npm ci
npx playwright install chromium
```

`npm run login` richiede inoltre `ENI_USERNAME` ed `ENI_PASSWORD` nel `.env`
locale; CAPTCHA e OTP richiedono comunque l'intervento nel browser. Queste
credenziali non sono richieste dall'integrazione Home Assistant.

1. Copia `.env.example` in `.env`.
2. Esegui il login manuale a My Eni nel browser.
3. Inserisci in `COOKIE` il valore completo dell'header `Cookie` del browser. Deve includere `GYSESSION`, `GAESA` e gli altri cookie `eniplenitude.com`.
4. Aggiorna, se necessario, `ACCOUNT_ID`, `SUPPLY_ID`, `POD`, `LOG_HASH` e `GRANULARITY`.

## Refresh sessione

```powershell
npm run refresh-session
```

Lo script chiama `GET /serviceDAp/api/c360/init` usando il cookie browser esistente. Se la risposta ruota `GAESA`, sostituisce soltanto quel valore in `COOKIE`; altrimenti conserva il cookie invariato. `GYSESSION` resta invariato.

## Monitoraggio sessione browser

```powershell
npm run monitor-session
```

Lo script apre un browser Playwright visibile: completa il login manualmente e premi Invio nel terminale. Il browser resta aperto per due ore e ricarica la pagina ogni cinque minuti. Le durate sono configurabili senza `.env`:

```powershell
$env:MONITOR_INTERVAL_MINUTES=5
$env:MONITOR_DURATION_MINUTES=120
npm run monitor-session
```

I report sono salvati in `monitor-logs/<timestamp>/`, cartella esclusa da Git. Gli eventi includono richieste, risposte, cookie ricevuti e snapshot di cookie/storage, ma non contengono valori di cookie, token, query string o storage: i valori sono rappresentati da impronte HMAC casuali valide soltanto per quella esecuzione.

`call-instant-production.mjs` applica automaticamente la stessa logica prima di richiamare `details` e `instant-production`.

Se `c360/init` restituisce un errore di sessione, `GYSESSION` e' scaduto o revocato: ripeti il login manuale e acquisisci un nuovo header `Cookie`.

## Configurazione

| Variabile | Descrizione |
| --- | --- |
| `COOKIE` | Valore completo dell'header `Cookie`, con `GYSESSION` e il corrente `GAESA`. |
| `ACCOUNT_ID` | Identificativo dell'account. |
| `SUPPLY_ID` | Identificativo della fornitura, usato dagli endpoint dei pannelli. |
| `POD` | Codice POD, usato dall'endpoint dei benefici. |
| `LOG_HASH` | Valore di `logHash`; se vuoto, resta comunque presente come `logHash=`. |
| `GRANULARITY` | Granularita' dei benefici; il valore predefinito e' `HOURLY`. |

## Endpoint e script

### Dettagli pannelli

```powershell
node --env-file=.env call-details.mjs
```

`GET /serviceDAp/api/pannelli-services/v1/accounts/<ACCOUNT_ID>/supply/<SUPPLY_ID>/details?logHash=<LOG_HASH>&channel=PORTAL`

La risposta contiene `panels`, un array di pannelli con campi quali `identifier`, `position`, `locationId`, `size`, `power`, `state` e `terminationDate`, oltre a `case48Hours`.

### Produzione istantanea

```powershell
node --env-file=.env call-instant-production.mjs
```

Lo script esegue automaticamente questi passaggi:

1. Richiama `details`.
2. Riduce ogni elemento di `panels` a `identifier`, `position` e `power`.
3. Invia l'array risultante nel body JSON di `POST /serviceDAp/api/pannelli-services/v1/accounts/<ACCOUNT_ID>/supply/<SUPPLY_ID>/instant-production?logHash=<LOG_HASH>&channel=PORTAL`.

Payload POST prodotto dallo script:

```json
[
  {
    "identifier": "SKU_ASSEMINI_L",
    "position": "Assemini (CA)",
    "power": "0.9"
  }
]
```

### Benefici

```powershell
node --env-file=.env call-benefits.mjs
```

`GET /serviceDAp/api/pannelli-services/v1/accounts/<ACCOUNT_ID>/pod/<POD>/benefits?granularity=<GRANULARITY>&logHash=<LOG_HASH>&channel=PORTAL`

La risposta contiene `panelBenefits`, un array con risparmio in euro e kWh, percentuali di utilizzo/copertura, `performance`, eventuali `tips` e `timeRange`.

`logHash` viene sempre incluso nella query string. Con `LOG_HASH` vuoto, l'URL contiene `logHash=&channel=PORTAL`.

`.env` e cookie non devono essere condivisi o inclusi in Git. Se il server restituisce `401`, `403` o `406`, esegui prima `npm run refresh-session`; acquisisci nuovamente il cookie dal browser solo se le API continuano a rifiutare la sessione.
