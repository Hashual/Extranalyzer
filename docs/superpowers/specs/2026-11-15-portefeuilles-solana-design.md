# Extranalyzer — suivi des portefeuilles Solana (design)

Date : 2026-11-15 · Statut : **validé par l'utilisateur** (2026-11-15)

## 1. Intention

L'utilisateur détient des cryptomonnaies sur plusieurs adresses Solana : jetons variés, xStocks (actions
tokenisées) et SOL placés en staking natif chez des validateurs (par ex. celui de Solana Mobile). Il veut :

1. **Voir ce que contient chaque portefeuille et sa valeur en euros**, depuis Extranalyzer, en saisissant
   simplement les adresses publiques.
2. **S'y retrouver dans son épargne** : chaque portefeuille devient une destination d'épargne
   (« Épargne / <nom du portefeuille> ») à laquelle il rattache les virements partis de sa banque. Il sait ainsi
   combien il a versé où, et peut le comparer à ce que le portefeuille vaut aujourd'hui.

Point de départ assumé (dit par l'utilisateur) : **le portefeuille contient des fonds, on part de là.** On ne
reconstitue pas l'origine des fonds. Certains portefeuilles ne seront jamais reliés à un virement bancaire. Pour
d'autres (notamment via Kraken), les débits bancaires peuvent l'être. Le rattachement **à la main** suffit, car
il y a peu de virements de ce type. L'automatisation est un plus.

Le design initial avait prévu cette extension (`2026-11-10-extranalyzer-refonte-design.md` § 5.10). Elle s'appuie
sur la notion de source externe introduite par Enable Banking (`2026-11-12-enable-banking-design.md`).

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Ajouter, renommer et retirer **plusieurs adresses** Solana depuis l'interface.
- Pour chaque portefeuille, afficher :
  - le SOL disponible ;
  - chaque jeton fongible, xStocks compris (programmes SPL Token et Token-2022) ;
  - chaque compte de staking natif rattaché à l'adresse : validateur, état, montant.
  Chaque ligne donne une quantité, un prix et une valeur en euros, avec le total du portefeuille et le total
  général.
- Actualiser automatiquement (synchro planifiée) et à la demande.
- Garder l'**historique de la valeur** de chaque portefeuille (un relevé à chaque actualisation) pour tracer une
  courbe.
- Créer une sous-catégorie « Épargne / <nom> » par portefeuille. Afficher sur la carte du portefeuille le **net
  versé depuis la banque** (versements − retraits classés dans cette sous-catégorie, depuis toujours) à côté de
  la valeur actuelle.

**Non-objectifs (première version)**
- Autres réseaux que Solana.
- Importer les opérations de la blockchain comme transactions : elles sont libellées en jetons, pas en euros, et
  fausseraient les dépenses, revenus et épargne de la vue d'ensemble.
- Appariement automatique débit bancaire ↔ arrivée sur la blockchain, et toute connexion à Kraken. Pour un
  rattachement automatique simple, le moteur de règles existant suffit (« libellé contient KRAKEN → Épargne /
  <portefeuille> »).
- Calcul fiscal, prix de revient, plus-values réalisées.
- Projection des récompenses de staking, onglet « Patrimoine » consolidé : plus tard.
- NFT.
- Toute écriture sur la blockchain : l'appli ne connaît que des adresses publiques, **jamais une clé privée**.

**Anti-objectifs (échec même si « ça marche »)**
- **Valeur fausse en silence.** Un jeton sans prix n'est jamais compté pour zéro sans le dire. Le total indique
  « hors N jetons sans prix ». Une actualisation en échec n'écrase pas le dernier relevé valide et l'erreur est
  visible.
- **Staking oublié.** Un portefeuille qui stake ne doit pas paraître presque vide.
- **Totaux bancaires faussés.** Les valeurs des portefeuilles n'entrent ni dans Dépenses, ni dans Revenus, ni dans
  Épargne de la vue d'ensemble : celles-ci restent des flux bancaires.
- **Secret exposé.** La clé Helius n'apparaît ni dans le dépôt, ni dans la base, ni dans les logs, ni dans
  l'interface.

## 3. Contraintes

- Celles du projet (refonte § 3 et Enable Banking § 4) :
  - données sur le Pi, aucune ressource chargée depuis internet par l'interface, aucune télémétrie ;
  - tests sans réseau (transport HTTP injectable et simulé) et avec des **adresses fictives** ;
  - aucune adresse réelle de l'utilisateur dans le dépôt ni dans la mémoire de l'assistant.
- Les adresses sont publiques mais **personnelles** : on ne les écrit pas dans les logs. L'interface les affiche
  en entier seulement dans le formulaire d'édition, et sinon raccourcies (`AbCd…WxYz`). Le masquage des montants
  existant (`privacy.js`) s'applique aux valeurs.
- Le conteneur ne sort que vers les API de la source (ici le domaine Helius), comme pour Enable Banking.
- Pi ARM64, 256 Mo : quelques adresses, deux synchros par jour. Volume négligeable.
- Aucune nouvelle dépendance Python : client HTTP en `urllib` comme `app/sources/enablebanking/client.py`.

## 4. Approches envisagées

| Approche | Pour | Contre |
|---|---|---|
| **A. Helius** (retenue) | Offre gratuite avec clé (1 M de crédits par mois, 2 requêtes DAS par seconde). `getAssetsByOwner` renvoie le SOL et tous les jetons fongibles, Token-2022 compris, avec un prix en USD pour les jetons vérifiés. Les comptes de staking passent par son RPC standard. | Prix absents pour les jetons peu liquides. Dépendance à un fournisseur, limitée : les appels de staking sont du RPC standard, et l'URL est configurable. |
| B. RPC Solana public + Jupiter pour les prix | Aucun fournisseur privilégié. | Le RPC public bride ou refuse `getProgramAccounts`, nécessaire pour le staking. Il faut assembler soldes, métadonnées et prix. Jupiter exige aussi une clé gratuite (l'ancien accès sans clé est abandonné). |
| C. Solscan Pro API | Portefeuille, staking et prix au même endroit. | Payant pour les fonctions utiles : environ 49 $ par mois ou plus selon les sources. Offre gratuite limitée. |

**Choix de l'utilisateur : A (Helius).**

## 5. Décisions de conception

### 5.1 Où ça vit dans le code

```
app/sources/solana/
├─ client.py     # appels Helius : DAS + RPC, transport injectable, erreurs typées (comme enablebanking/client.py)
├─ holdings.py   # réponse brute -> positions (SOL, jetons, staking) en Decimal ; conversion USD -> EUR
├─ wallets.py    # ajout / renommage / retrait d'un portefeuille, sous-catégorie d'épargne, actualisation
└─ runtime.py    # client + verrou d'actualisation + tick planifié (comme enablebanking/runtime.py)
app/tabs/banque_wallets.py       # APIRouter inclus dans celui de banque.py : /api/tabs/banque/wallets/...
app/web/tabs/banque-wallets.js   # section « Portefeuilles Solana », importée par banque.js
docker-compose.solana.yml        # secret et variables, activé via COMPOSE_FILE (comme docker-compose.banque.yml)
```

### 5.2 Onglet : fusion dans « Banque » (choix de l'utilisateur)

Les portefeuilles sont une **section de l'onglet Banque**, « Portefeuilles Solana », placée sous les banques
connectées et au-dessus de « Synchronisations récentes ».
- `ENABLED_TABS` ne change pas.
- Le titre de l'onglet reste « Banque ». Le renommer, par exemple en « Comptes », est un changement d'une ligne.
  *(choix du concepteur)*
- Les deux parties sont **indépendantes** :
  - chacune a sa configuration ;
  - chacune a son écran « non configuré » ;
  - chacune a son runtime et son `Scheduler`.
  Une banque non configurée n'empêche pas d'utiliser les portefeuilles, et inversement.
- `banque.py` reste centré sur Enable Banking. Les routes des portefeuilles vivent dans `banque_wallets.py`, monté
  sous `/api/tabs/banque/wallets/`. Côté client, `banque.js` délègue le rendu de la section à
  `banque-wallets.js`.

Esquisse de la section :

```
Portefeuilles Solana                Total : 4 812,40 €  (hors 2 jetons sans prix)   [Actualiser]
                                    Dernière actualisation : aujourd'hui 08:00
┌ Principal  AbCd…WxYz ───────────────────────── 3 902,10 € ── [Renommer] [Retirer] ┐
│ Net versé depuis la banque : 2 500,00 €  (Épargne / Principal)                     │
│ SOL disponible       1,2400 SOL     152,30 €/SOL     188,85 €                      │
│ Staking · Solana Mobile (Vote…k3Fz)  actif   22,1050 SOL   3 366,90 €              │
│ USDC                 120,00         0,86 €           103,20 €                      │
│ TSLAx (xStock)       0,5000         ...              243,15 €                      │
│ ▸ 2 jetons sans prix                                                               │
│ [courbe de la valeur dans le temps]                                                │
└────────────────────────────────────────────────────────────────────────────────────┘
[+ Ajouter une adresse]   (nom + adresse)
```

Non configuré (pas de clé) : la section explique comment activer, comme le fait la partie bancaire.

### 5.3 Données

Tables nouvelles (migration v7), indépendantes de `accounts` :

- `wallets(id, address UNIQUE, label, savings_category_id → categories, created_at, last_sync_at, last_error)`.
- `wallet_snapshots(id, wallet_id, taken_at, total_eur_cents, unpriced_count, status 'ok'|'error', message)`.
  Une ligne par actualisation.
- `wallet_positions(snapshot_id, kind 'sol'|'token'|'stake', mint, symbol, name, amount TEXT, decimals,
  price_eur TEXT NULL, value_eur_cents NULL, stake_account, vote_account, stake_state)`.

**Écart avec la refonte § 5.10**, qui prévoyait des comptes `accounts.kind = 'wallet'` *(choix du concepteur)* :
un portefeuille n'a pas d'opérations en euros. Le mettre dans `accounts` le ferait apparaître dans le sélecteur
de comptes, les relevés, les règles par compte et l'appariement, sans rien y apporter. Le lien avec l'épargne
passe par `savings_category_id`.

Quantités et prix en `Decimal` sérialisé en texte ; valeurs en centimes d'euro, comme le reste de l'appli.

### 5.4 Lecture d'un portefeuille (Helius)

1. **SOL et jetons** : DAS `getAssetsByOwner` avec l'affichage des jetons fongibles et du solde natif activé.
   On garde les actifs fongibles (y compris Token-2022, donc les xStocks) et le SOL natif. Les NFT sont ignorés.
   Pagination jusqu'au bout. Le prix USD vient de `token_info.price_info` quand il existe (cache Helius ≤ 10 min).
2. **Staking natif** : RPC `getProgramAccounts` sur le programme Stake avec un filtre `memcmp` sur l'autorité de
   retrait = adresse. Un second appel filtre sur l'autorité de staking, et les résultats sont dédoublonnés. Pour
   chaque compte, on lit :
   - le montant en lamports, c'est-à-dire la totalité du compte, récompenses accumulées comprises ;
   - le compte de vote du validateur ;
   - les époques d'activation et de désactivation.
   L'état (`activating`, `active`, `deactivating`, `inactive`) est déduit de l'époque courante (`getEpochInfo`).
   Valorisé au prix du SOL.
3. **Nom du validateur** : afficher le compte de vote raccourci. L'utilisateur peut lui donner un nom (table
   `validator_labels(vote_account PRIMARY KEY, label)`), qui vaut pour tous ses portefeuilles. *(choix du
   concepteur : la lecture automatique des noms on-chain est hors périmètre)*
4. **Conversion en euros** : `eur = usd / prix_usd(EURC)`, avec le prix de l'EURC de Circle (adresse de mint officielle, à vérifier dans la documentation de Circle) lu
   dans la même réponse ou par `getAsset`. Sans prix EURC, l'actualisation échoue proprement : on n'invente pas
   de taux.
5. **Seconde source de prix (xStocks)** *(choix du concepteur, proposé à l'utilisateur)* :
   - Première étape de l'implémentation : vérifier avec un vrai appel que Helius renvoie un prix pour les xStocks
     de l'utilisateur.
   - Si c'est le cas, Helius reste la seule source.
   - Sinon, on ajoute Jupiter Price v3 (`api.jup.ag/price/v3`, clé gratuite en second secret Docker, facultatif)
     comme complément, **uniquement** pour les mints auxquels Helius ne donne pas de prix. Helius reste prioritaire.
   - Le prix d'une position mémorise sa source.
6. **Jetons sans prix** : quantité affichée, valeur vide, comptés dans `unpriced_count`. On peut **masquer** un
   mint jugé indésirable, par exemple un jeton de spam reçu sans le demander (table `hidden_mints`). *(choix du
   concepteur)*

### 5.5 Actualisation

- **Planifiée** : un `Scheduler` (`app/sources/scheduler.py`) dédié, à la même période que la synchro bancaire
  (`EXTRANALYZER_SYNC_HOURS`). Il ne démarre que si la clé est configurée.
- **À la demande** : bouton « Actualiser » (tous les portefeuilles), et actualisation immédiate à l'ajout d'une
  adresse.
- **Déroulé** :
  1. tous les appels réseau d'abord, hors du verrou d'écriture ;
  2. puis une transaction SQLite par portefeuille, qui écrit le relevé et ses positions.
  Une erreur sur un portefeuille donne un relevé `error` avec un message, sans toucher au dernier relevé `ok` ni
  aux autres portefeuilles.
- Une seule actualisation à la fois (verrou non bloquant, comme `BankRuntime.sync`).
- **Rétention** : tous les relevés sont gardés (deux par jour ≈ 730 lignes par an et par portefeuille,
  négligeable).

### 5.6 Lien avec l'épargne

- À l'ajout d'un portefeuille, on crée la sous-catégorie « Épargne / <nom> » sous la racine d'épargne. On réutilise
  la logique de racine de `app/classify/savings_target.py`. Si une sous-catégorie du même nom existe déjà, on la
  réutilise.
- Renommer le portefeuille renomme sa sous-catégorie, si elle porte encore l'ancien nom. *(choix du concepteur)*
- Retirer le portefeuille supprime ses relevés et positions, mais **laisse la sous-catégorie** et les virements
  qui y sont classés. Supprimer la catégorie reste une action de l'onglet Catégories. *(choix du concepteur)*
- **Rattacher un virement** : par les moyens existants. On reclasse le débit dans « Épargne / <nom> »
  (Transactions, Catégories, À classer), ou on crée une règle (par ex. KRAKEN). Rien de nouveau côté classement.
- **Carte du portefeuille** : affiche le « net versé depuis la banque ». *(choix du concepteur)*
  - Calcul : **débits** classés dans cette sous-catégorie (versements) − **crédits** classés dans cette
    sous-catégorie (retraits), toutes dates, **sur tous les comptes** de l'utilisateur, jambes miroir exclues.
  - Ce calcul ne reprend pas tel quel `SIGNED_AMOUNT['savings']` de `app/analytics/totals.py` : celui-ci inverse le
    signe sur un compte d'épargne, et le périmètre par défaut ne garde que les comptes courants. Le cas visé est
    simple : un débit vers un portefeuille ou Kraken est un versement, quel que soit le compte d'où il part.
  - L'onglet Épargne existant affiche déjà cette destination sans changement, avec son propre périmètre de comptes.
    Les deux chiffres sont identiques quand les virements partent du compte courant, ce qui est le cas attendu.

### 5.7 Configuration et secrets

- `docker-compose.solana.yml` : secret `helius_key` (fichier `./secrets/helius.key`) monté sur
  `/run/secrets/helius_key`, et variable `EXTRANALYZER_HELIUS_KEY_FILE`.
- Activé en ajoutant ce fichier à `COMPOSE_FILE` dans `.env`, à côté de `docker-compose.banque.yml`.
- `EXTRANALYZER_HELIUS_URL` (par défaut `https://mainnet.helius-rpc.com`) permet de changer de fournisseur RPC.
- La clé n'est jamais écrite en base, dans les logs ni dans les réponses d'API. Les messages d'erreur sont
  nettoyés de l'URL, qui contient la clé en paramètre.
- README : section « Portefeuilles Solana (Helius) » qui explique comment créer la clé gratuite, poser le secret
  et activer.

### 5.8 Tests

- Client : réponses Helius simulées (DAS paginé, Token-2022, jeton sans prix, NFT ignoré, erreurs HTTP et
  quota).
- Staking : comptes simulés dans chaque état, dédoublonnage staker/retrait.
- Conversion EUR, cas sans EURC, arrondis.
- Actualisation : échec d'un portefeuille sans effet sur les autres ni sur le dernier relevé `ok`.
- API de l'onglet : ajout (validation d'adresse base58, 32 octets), doublon refusé, renommage et sous-catégorie,
  retrait, masquage d'un mint, absence de la clé dans toute réponse.
- Registre d'onglets : l'onglet se charge, et l'appli démarre sans clé (onglet « non configuré »).

## 6. Laissé au builder

- Forme exacte des requêtes Helius (`options` ou `displayOptions` selon l'API en vigueur) et coût en crédits :
  vérifier dans la documentation au moment de l'implémentation.
- Offsets exacts du filtre `memcmp` des comptes Stake (autorité de staking et de retrait).
- Validation d'adresse (décodage base58 sans dépendance).
- Style de la courbe (Chart.js déjà embarqué) et période affichée par défaut.
- Délais réseau, nouvelles tentatives et gestion du quota (`429`).

## 7. Points à trancher avec l'utilisateur

- Aucun à ce stade. L'emplacement de l'onglet est tranché (fusion dans Banque, § 5.2). La seconde source de prix
  dépend de la vérification décrite au § 5.4.
