# Sviluppo e test

[← README](../README.md)

## Struttura

- `custom_components/plenitude_panels/`: tutti i file necessari all'integrazione.
- `tests/`: suite esistente, con fixture e SQLite in memoria per le statistiche.
- `docs/`: documentazione utente e tecnica.
- `tools/node/`: prototipi indipendenti; non sono dipendenze Python o runtime.
- `tools/generate_brand.py`: generazione riproducibile delle icone PNG originali.

## Ambiente di test

La baseline riproducibile è **Python 3.13 e Home Assistant 2025.10.4**.
Usa preferibilmente Linux, come nei runner CI: l'installazione completa di
Home Assistant su Windows può richiedere compilatori per le dipendenze native.

```sh
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# PowerShell, in alternativa:
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

I test non richiedono credenziali reali o chiamate al portale. Verificano sessioni
browser, recupero produzione, presentazione delle anteprime e transazioni sulle
statistiche di Recorder. Su GitHub, HACS, Hassfest e test vengono eseguiti solo
all'avvio manuale del workflow **Publish release**, prima della pubblicazione.
Per verificare modifiche durante lo sviluppo usa il comando locale riportato sopra.

## Versioni

Segui [AGENTS.md](../AGENTS.md): per ogni modifica al componente aggiorna
`manifest.json` e aggiungi una voce iniziale allo stesso numero nel changelog.
Usa PATCH per correzioni, MINOR per funzionalità compatibili e MAJOR per modifiche
incompatibili o migrazioni di configurazione. I tag delle release devono
corrispondere alla versione del manifest, per esempio `v0.10.1`.

Tag e release sono creati automaticamente solo dopo il successo dei controlli:
vedi la [procedura di pubblicazione](pubblicazione-hacs.md#2-pubblicare-una-release).

## Icone

Le icone non riproducono marchi Plenitude o Home Assistant. Il disegno originale
del progetto rappresenta un pannello e il sole, ed è distribuito sotto MIT.
Per rigenerarle, dalla radice:

```sh
python tools/generate_brand.py
```

Il generatore usa solo la libreria standard e produce `icon.png` (256×256)
e `icon@2x.png` (512×512) nella cartella `brand/` dell'integrazione.
