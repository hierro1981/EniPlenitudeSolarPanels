# Pubblicazione su HACS

[← README](../README.md)

## Stato della preparazione

Il repository è stato predisposto con:

- un'unica integrazione in `custom_components/plenitude_panels/`;
- `hacs.json` alla radice, con paese `IT`;
- manifest con versione, documentazione, issue tracker e codeowner;
- icone locali originali nella cartella `brand/`;
- README e licenza MIT;
- workflow manuale `.github/workflows/validate.yml` per HACS Action, Hassfest,
  test e pubblicazione della release solo dopo il successo dei controlli.

Questi file **non equivalgono all'accettazione nel catalogo predefinito**.
Le validazioni remote e le impostazioni GitHub vanno verificate dopo il push.

## 1. Abilitare il download come repository personalizzato

Pubblica il repository su GitHub, rendilo pubblico e configura nelle impostazioni:

- **Descrizione**, per esempio: `Integrazione Home Assistant per i pannelli solari Plenitude, con dashboard Energia e recupero produzione.`
- **Topic**, per esempio: `home-assistant`, `hacs`, `custom-integration`,
  `plenitude`, `solar-energy`.
- **Issues** abilitate e GitHub Actions abilitate.

Prima della prima pubblicazione verifica le impostazioni del repository.
Il job HACS non contiene esclusioni (`ignore`) e controlla anche i metadati
del repository remoto. Hassfest e i test controllano il commit selezionato
all'avvio della pubblicazione.

Gli utenti potranno aggiungere il repository con tipo **Integrazione**, come
descritto nel README. HACS usa il ramo predefinito se non esistono release;
per distribuire versioni stabili è preferibile creare release GitHub.

## 2. Pubblicare una release

Il workflow parte **solo quando avvii manualmente una pubblicazione**: non viene
eseguito su push, merge, pull request o a intervalli programmati. Non creare prima
la release dall'interfaccia **Releases**: sarà il workflow a crearla dopo i controlli.

1. Aggiorna `manifest.json` e la prima voce di `CHANGELOG.md` alla stessa versione
   stabile `X.Y.Z`, per esempio `0.10.1`, con note non vuote.
2. Fai commit e push delle modifiche sul ramo predefinito del repository
   (attualmente `main`). Il file del workflow deve essere pubblicato su quel ramo
   per comparire tra le azioni eseguibili manualmente.
3. Apri **Actions > Publish release > Run workflow**, seleziona il ramo predefinito
   e avvia l'esecuzione. L'avvio da altri rami o tag viene rifiutato.
4. Il workflow verifica versione e changelog e controlla che il tag e la release
   corrispondenti non esistano già. Poi esegue HACS, Hassfest e i test Python.
5. Solo se tutti i controlli passano crea una **release GitHub completa**, con tag
   `vX.Y.Z`, sullo **stesso commit selezionato all'avvio**. Le note sono estratte
   dalla prima voce del changelog e la release viene contrassegnata come latest.

In caso di errore non viene creata la release: correggi il problema, pubblica le
correzioni sul ramo predefinito e avvia una nuova esecuzione. Per una versione
già distribuita occorre incrementare il numero, senza sostituire tag esistenti.
Le pubblicazioni sono serializzate per evitare esecuzioni simultanee.

Il job di pubblicazione usa il `GITHUB_TOKEN` automatico con `contents: write`;
gli altri job hanno accesso in sola lettura. Non serve configurare un PAT.
Eventuali regole GitHub che limitano la creazione dei tag devono consentire
l'operazione a GitHub Actions.

Non è necessario allegare ZIP personalizzati: `hacs.json` usa il layout standard
e non dichiara `zip_release`. HACS scarica il contenuto del componente dal repository.

## 3. Richiedere l'inclusione nel catalogo predefinito

Il proprietario o un contributore principale può aprire la richiesta:

1. Verifica l'installazione reale come repository personalizzato e i requisiti
   ufficiali aggiornati.
2. Avvia la pubblicazione come descritto sopra: **HACS Action deve passare senza
   errori o esclusioni**, insieme a Hassfest e ai test, prima della creazione
   automatica della release richiesta.
3. Crea un fork di [`hacs/default`](https://github.com/hacs/default) e un nuovo
   ramo a partire da `master`.
4. Nel file [`integration`](https://github.com/hacs/default/blob/master/integration)
   aggiungi `hierro1981/EniPlenitudeSolarPanels` rispettando l'ordine alfabetico.
5. Apri una PR modificabile dai manutentori e compila integralmente il template.

La revisione controlla, tra l'altro, licenza open source riconosciuta, icone,
release, metadati e assenza di conflitti con domini Home Assistant core.
Per un servizio limitato all'Italia il paese deve comparire anche nel
`hacs.json` della release: questo repository usa `"country": "IT"`.

L'approvazione non è automatica: la documentazione HACS segnala che l'attesa può
durare mesi. Dopo il merge il repository entra nella scansione programmata.
Il download come repository personalizzato resta il percorso utilizzabile
durante l'attesa.

## Fonti ufficiali

Requisiti consultati durante il riordino; ricontrollarli prima di presentare la PR:

- [Requisiti generali e hacs.json](https://www.hacs.xyz/docs/publish/start/)
- [Requisiti delle integrazioni](https://www.hacs.xyz/docs/publish/integration/)
- [GitHub Action HACS](https://www.hacs.xyz/docs/publish/action/)
- [Inclusione nel catalogo predefinito](https://www.hacs.xyz/docs/publish/include/)
- [Icone locali delle integrazioni](https://contributors.home-assistant.io/core/integrations/brands/)
