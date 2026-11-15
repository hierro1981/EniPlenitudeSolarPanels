# Sito del prodotto

Il sito è online su **[solar.ramaya.org](https://solar.ramaya.org/)**,
pubblicato tramite GitHub Pages dal contenuto della cartella `site/`.
Riprende l’identità visiva di Ramaya Software e la struttura di presentazione di
FM Simple Editor. Non richiede build, npm, framework o servizi di analisi.

## Pubblicare gli aggiornamenti

1. Modifica i file in `site/` e verifica il risultato in anteprima locale.
2. Esegui commit e push su `main`: il workflow **Deploy product website**
   pubblica automaticamente le modifiche al sito.
3. Controlla l’esito del workflow nella scheda **Actions** e verifica il sito
   online. Se necessario, puoi avviare lo stesso workflow manualmente su `main`.

Il workflow si avvia sui push a `main` che modificano `site/` o
`.github/workflows/pages.yml`, oppure manualmente. Le sole modifiche a
`README.md` o a questa guida non avviano il deployment.
Carica solo `site/`. Solo il job di deployment dispone di `pages: write` e
`id-token: write`; il job di preparazione ha accesso in lettura.
Il workflow **Publish release** rimane separato e manuale: non avviarlo per
pubblicare o verificare il sito. Gli aggiornamenti del solo sito non richiedono
un incremento della versione dell’integrazione.

## Configurazione GitHub Pages e DNS (riferimento)

Pages e il dominio personalizzato sono già attivi. I passaggi seguenti restano
come riferimento per ripristinare o riconfigurare la pubblicazione:
aggiungere i file al repository non configura automaticamente Pages o il DNS.

1. Su GitHub apri **Settings → Pages** del repository
   `hierro1981/EniPlenitudeSolarPanels`.
2. In **Build and deployment → Source** seleziona **GitHub Actions**.
3. Pubblica su `main` i file del sito e `.github/workflows/pages.yml`.
   Se sono già presenti, avvia manualmente **Deploy product website** da
   **Actions**, selezionando `main`.
4. Nel pannello DNS di `ramaya.org` aggiungi un record:

   | Tipo | Nome | Destinazione |
   | --- | --- | --- |
   | CNAME | `solar` | `hierro1981.github.io` |

   La destinazione non include protocollo, percorso o nome del repository.
   Rimuovi eventuali record incompatibili soltanto per lo stesso host `solar`.
5. In **Settings → Pages → Custom domain** inserisci `solar.ramaya.org` e
   attendi la verifica DNS. Il file `site/CNAME` documenta il dominio desiderato,
   ma con un deployment via Actions non sostituisce questa impostazione.
6. Attendi l’emissione del certificato e attiva **Enforce HTTPS**.
7. Verifica homepage, `privacy.html`, icona, immagine social e collegamenti HACS.
   Se disponibile, verifica anche la proprietà del dominio nelle impostazioni
   Pages dell’account GitHub seguendo le istruzioni TXT fornite da GitHub.

## Anteprima locale

Dalla radice del repository:

```powershell
python -m http.server 8000 --directory site --bind 127.0.0.1
```

Apri `http://127.0.0.1:8000/`. Interrompi con `Ctrl+C`.

## Manutenzione

- Mantieni funzioni, requisiti e limiti coerenti con il `README.md` e le guide.
- Il sito non fissa la versione corrente: rimanda a release e changelog,
  evitando che il numero diventi obsoleto a ogni rilascio.
- Nessun dato dimostrativo deve essere presentato come misura reale.
- Non inserire cookie, credenziali, ID personali o screenshot con dati sensibili.
- Non aggiungere tracker, font remoti o embed senza aggiornare l’informativa.
- Verifica navigazione da tastiera, apertura delle FAQ, contrasto, layout mobile,
  collegamenti e assenza di errori console prima della pubblicazione.
- Per un altro dominio aggiorna `CNAME`, canonical, metadati social,
  `robots.txt` e `sitemap.xml`, oltre alle impostazioni Pages e DNS.

Il monogramma `site/assets/logo-rs.svg` proviene dalla homepage Ramaya Software.
L’immagine social è già inclusa; per rigenerarla su Windows puoi eseguire
`powershell -File tools/generate_site_social.ps1`. Questo strumento facoltativo
non è una dipendenza del sito o dell’integrazione.

L’inserimento della scheda progetto nella homepage `ramaya.org` è una modifica
separata, da effettuare nel repository `hierro1981/ramaya.org`.
