# Extranalyzer — récupération automatique des comptes via Enable Banking (design)

Date : 2026-11-12 · Statut : **brouillon, à valider par l'utilisateur**

## 1. Intention

Aujourd'hui, chaque opération entre dans Extranalyzer par le dépôt manuel d'un relevé PDF mensuel. On veut que les
opérations du compte courant et des livrets arrivent **seules**, par l'open banking (DSP2), via l'agrégateur
Enable Banking. **L'API remplace les PDF pour la suite** (choix de l'utilisateur) : les PDF déjà importés restent
l'historique, l'API prend le relais à partir du dernier relevé de chaque compte.

Cette fonctionnalité est la première **source de données externe**. Sa conception doit resservir telle quelle pour
Kraken (session ultérieure, hors périmètre ici).

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
- Connecter une fois sa banque (Crédit Mutuel) depuis l'interface, puis recevoir les nouvelles opérations sans
  intervention, classées et appariées par la chaîne existante.
- Rattacher chaque compte bancaire de l'API à un compte déjà connu des PDF (pas de doublon de compte).
- Ne jamais compter deux fois une opération présente à la fois dans un PDF et dans l'API.
- Prévenir à l'avance que le consentement bancaire va expirer, et signaler une connexion rompue.
- Contrôler les soldes à chaque synchro et signaler un écart.

**Non-objectifs**
- Paiements ou toute écriture vers la banque : lecture seule (service AIS uniquement).
- Plusieurs banques ou plusieurs utilisateurs simultanés : une seule banque connectée suffit (le modèle de données
  n'interdit pas d'en ajouter une).
- Kraken, portefeuilles crypto, vue des actifs.
- Fusion approximative d'opérations PDF et API (rapprochement par libellé ou date voisine) : rendue inutile par la
  frontière de date (§ 5.5).
- Import des opérations « en attente » (non comptabilisées).

**Anti-objectifs (échec même si « ça marche »)**
- **Doublons silencieux** entre PDF et API, ou entre deux synchros.
- **Chiffres faux en silence** : un écart de solde doit être visible.
- **Secrets exposés** : clé privée, jeton de session ou IBAN complet dans le dépôt, les logs ou l'interface.
- **Panneau exposé sur internet** : la redirection bancaire ne doit pas imposer d'ouvrir l'appli au public.
- **Opérations perdues** : une synchro en échec ne doit pas faire sauter de période.

## 3. Ce que dit la documentation d'Enable Banking

Sources consultées le 2026-11-12. Les points non trouvés sont signalés comme tels.

| Sujet | Constat | Source |
|---|---|---|
| Enregistrement | Application créée dans le Control Panel (`enablebanking.com/cp/applications`) : environnement Sandbox ou Production, nom, liste blanche d'URL de redirection. | [quick-start](https://enablebanking.com/docs/api/quick-start) |
| Usage personnel | Une application de production démarre « Inactive » ; « Activate by linking accounts » la passe en mode **restreint** : usage individuel non commercial, données des seuls comptes liés. Lier un compte n'autorise pas l'accès : il faut quand même faire l'autorisation API. Un compte non lié ressort en liste vide. | [linked-accounts](https://enablebanking.com/docs/api/linked-accounts/), [FAQ](https://enablebanking.com/docs/faq/) |
| Prix | La FAQ parle d'une tarification au volume avec minimum mensuel ; la gratuité du mode restreint **n'est pas trouvée** dans la doc (page /pricing en 404). À vérifier par l'utilisateur dans le Control Panel. | [FAQ](https://enablebanking.com/docs/faq/) |
| Clé | Paire RSA générée par le navigateur à l'enregistrement, clé privée téléchargée en `<application-id>.pem` (variante : certificat public importé, exemple `openssl genrsa 4096`). | [quick-start](https://enablebanking.com/docs/api/quick-start), [référence](https://enablebanking.com/docs/api/reference/) |
| Jeton | JWT RS256 (seul algorithme accepté), en-tête `typ=JWT`, `kid=<application-id>` ; charges `iss="enablebanking.com"`, `aud="api.enablebanking.com"`, `iat`, `exp` ; durée maximale 86 400 s ; envoyé en `Authorization: Bearer`. Base : `https://api.enablebanking.com`. | quick-start, référence, FAQ |
| Flux | `GET /aspsps` (banques, dont `maximum_consent_validity` en secondes) → `POST /auth` (`access.valid_until`, `aspsp{name,country}`, `state`, `redirect_url`, `psu_type`) → réponse `url` → retour sur `redirect_url` avec `code` et `state` (ou `error`, `error_description`) → `POST /sessions {code}` → `session_id`, `accounts[]` (`uid`, `account_id.iban`, `name`, `currency`, `cash_account_type` CACC/SVGS/…, `identification_hash`), `access.valid_until`. | référence |
| URL de redirection | Doit figurer dans la liste blanche (erreur `REDIRECT_URI_NOT_ALLOWED`). Les exemples de la référence montrent `http://` et `https://`. **Non trouvé** : si une IP privée en `http://` est acceptée. | quick-start, référence |
| Données | `GET /accounts/{uid}/balances` (`balance_type` CLBD, ITBD, CLAV, ITAV…, `reference_date`) ; `GET /accounts/{uid}/transactions` (`date_from`, `date_to`, `continuation_key`, `transaction_status`, `strategy=default\|longest`) → `transactions[]` + `continuation_key`. Transaction : `transaction_id`, `entry_reference`, `transaction_amount{amount,currency}`, `credit_debit_indicator` CRDT/DBIT, `status` BOOK/PDNG/…, `booking_date`, `value_date`, `transaction_date`, `remittance_information[]`, `creditor`/`debtor`, `bank_transaction_code`. `DELETE /sessions/{id}` ferme la session. | référence |
| Consentement | `valid_until` ≤ maintenant + `maximum_consent_validity` de la banque, « 180 jours pour la majorité ». Pas de rafraîchissement : nouvelle autorisation à l'échéance. Une session peut expirer avant (`EXPIRED_SESSION`, 401). | référence, FAQ |
| Limites | Beaucoup de banques limitent à 4 consultations par jour sans l'utilisateur présent ; en-têtes PSU pour signaler sa présence ; dépassement : 429 `ASPSP_RATE_LIMIT_EXCEEDED`, attendre 6 h. | FAQ |
| Historique | Historique complet souvent disponible environ une heure après l'autorisation seulement, puis 90 jours chez beaucoup de banques ; `strategy=longest` pour remonter au plus loin. | FAQ |
| Crédit Mutuel | « Crédit Mutuel » et « CIC » figurent sur la page France : un même flux par redirection pour toutes les caisses régionales. La prise en charge de `psu_type=personal` n'y est pas précisée (vérifiable par `GET /aspsps?country=FR`). | [markets/fr](https://enablebanking.com/docs/markets/fr/) |
| Exemple officiel | Python avec `requests` et PyJWT. | [enablebanking-api-samples](https://github.com/enablebanking/enablebanking-api-samples/blob/master/python_example/account_information.py) |

## 4. Contraintes

- Toutes celles de la spec de refonte (§ 3 de `2026-11-10-extranalyzer-refonte-design.md`) : données sur le Pi,
  accès par le VPN WolfNet uniquement (`http://10.10.10.95:8000`), image Alpine arm64 minimale, dépendances figées.
- **Aucune nouvelle dépendance** : `cryptography` (déjà dans l'image via pdfminer) signe le JWT en RS256 ;
  `urllib.request` (bibliothèque standard) fait les appels HTTPS.
- Le conteneur a besoin d'un accès sortant vers `api.enablebanking.com` (et c'est tout).
- Les tests n'appellent jamais le réseau : le client HTTP est injectable et simulé.
- Une autre session ajoute la suppression des relevés importés (`app/importer/service.py`, onglet Relevés,
  éventuellement le schéma) : limiter les retouches de ces fichiers au strict nécessaire et rebaser avant fusion.

## 5. Décisions de conception

### 5.1 Approches envisagées

| Approche | Pour | Contre |
|---|---|---|
| **A. Opérations directement dans `transactions`, sans section** (retenue) | Le schéma le prévoit déjà (`section_id` nullable « pour des sources non-PDF ») ; l'appariement accepte déjà les lignes sans section ; contrôle de solde non bloquant possible ; réutilisable pour Kraken. | Pas de réconciliation bloquante : remplacée par un avertissement. |
| B. Une pseudo-section par synchro | Réutilise la réconciliation bloquante et l'onglet Relevés. | Les soldes API incluent parfois des opérations en attente : faux écarts bloquants ; la logique de conflit par date de relevé n'a pas de sens pour des synchros qui se chevauchent. |
| C. Zone d'attente à valider | Le plus prudent. | Réintroduit une action manuelle à chaque synchro, contraire au but. |

**Choix de l'utilisateur : A.**

### 5.2 Architecture : une notion générale de source

```
app/
├─ sources/
│  ├─ base.py            # FetchedTransaction, FetchedBalance, contrat d'une source
│  ├─ service.py         # écriture générique : frontière, identité, insertion, contrôle de solde, classement
│  ├─ scheduler.py       # thread de synchro périodique
│  └─ enablebanking/
│     ├─ jwt.py          # jeton RS256 signé avec cryptography
│     ├─ client.py       # appels HTTP (transport injectable), pagination, erreurs typées
│     └─ link.py         # connexion, retour de la banque, rattachement des comptes, synchro d'un lien
├─ tabs/banque.py        # onglet « Banque »
└─ web/tabs/banque.js, web/bank-callback.html
```

- `sources/service.py` ne connaît rien d'Enable Banking : il reçoit, pour un compte Extranalyzer, une liste de
  `FetchedTransaction(external_id | None, op_date, value_date, label, amount: Decimal, raw: dict)` et un solde
  facultatif. Kraken fournira les mêmes objets.
- `sources/enablebanking/` traduit l'API en ces objets.

### 5.3 Secrets et configuration

- **Clé privée** : secret Docker `enablebanking_key` (fichier `./secrets/enablebanking.pem`, déjà couvert par
  `.gitignore`), monté en `/run/secrets/enablebanking_key`. Jamais en base, jamais loggée.
- **Variables d'environnement** :
  - `EXTRANALYZER_EB_APP_ID` : identifiant de l'application (non secret, sert de `kid`) ;
  - `EXTRANALYZER_EB_KEY_FILE` (défaut `/run/secrets/enablebanking_key`) ;
  - `EXTRANALYZER_EB_REDIRECT_URL` : URL de retour déclarée dans le Control Panel ;
  - `EXTRANALYZER_SYNC_HOURS` (défaut 12 ; 0 désactive la synchro planifiée).
- **Sans identifiant ou sans clé**, la fonctionnalité est désactivée : l'appli démarre normalement et l'onglet
  Banque explique quoi configurer.
- Le jeton JWT est créé à la demande, valable 1 h, régénéré avant son expiration.
- L'utilisateur crée lui-même l'application, la clé et le lien de ses comptes ; le README décrit la marche à suivre.

### 5.4 Connexion à la banque et URL de redirection

1. Onglet Banque → « Connecter ma banque » : le serveur lit `GET /aspsps?country=FR&psu_type=personal`, retient
   l'entrée « Crédit Mutuel » (le nom est configurable si l'API l'écrit autrement), demande
   `valid_until = maintenant + maximum_consent_validity`, et envoie `POST /auth` avec un `state` aléatoire
   (32 octets) enregistré en base, à usage unique, valable 30 minutes.
2. Le navigateur part vers l'`url` de la banque ; l'utilisateur s'authentifie.
3. **Retour** : la banque renvoie vers `EXTRANALYZER_EB_REDIRECT_URL`, prévue comme
   `http://10.10.10.95:8000/bank/callback`. Le navigateur de l'utilisateur est sur le VPN : la redirection
   fonctionne sans exposer l'appli, Enable Banking n'appelle jamais cette adresse lui-même.
   - Le cookie de session est `SameSite=Strict` : il **n'est pas envoyé** lors d'une navigation venue du site de la
     banque. `/bank/callback` est donc une page statique (sans donnée) dont le script lit `code` et `state` dans
     l'URL puis les envoie par `POST /api/bank/session` : cette requête part de la page de l'appli, le cookie suit
     et l'authentification habituelle s'applique.
   - **Repli** si le Control Panel refuse `http://10.10.10.95` : déclarer une autre URL (par exemple
     `https://localhost/bank/callback`) ; la page d'erreur affichée par le navigateur contient `code` et `state`
     dans sa barre d'adresse ; l'utilisateur colle cette adresse dans un champ de l'onglet Banque, qui l'envoie à
     la même route.
4. Le serveur vérifie le `state` (connu, non expiré, non utilisé), appelle `POST /sessions`, enregistre le lien
   (`session_id`, banque, `valid_until`) et les comptes renvoyés, puis rattache les comptes (§ 5.6) et lance une
   première synchro immédiate (l'historique long n'est souvent disponible que dans l'heure qui suit).
5. Un `error` dans le retour est affiché tel quel (masqué des numéros) et le `state` est consommé.

**Renouvellement** : refaire « Connecter ma banque ». Les comptes du nouveau lien sont retrouvés par
`identification_hash` (sinon IBAN) et reprennent leur rattachement ; l'ancien lien passe `replaced` et sa session
est fermée (`DELETE /sessions/{id}`, échec toléré).

**Déconnexion** : bouton dans l'onglet ; ferme la session, garde toutes les opérations importées.

### 5.5 Frontière PDF / API (dédoublonnage)

- Pour chaque compte Extranalyzer, la **frontière** est la date du dernier relevé PDF `ok` de ce compte
  (`MAX(sections.statement_date)`), recalculée à chaque synchro. Seules les opérations API dont la date est
  **strictement postérieure** sont importées. Les deux sources ne se recouvrent donc jamais ; aucun rapprochement de
  libellés n'est nécessaire.
- Compte sans aucun relevé PDF (nouveau compte) : pas de frontière, tout l'historique disponible est importé.
- **Garde-fou dans l'autre sens** : l'import d'un PDF dont une section porte sur un compte ayant déjà des opérations
  API **dans la période de la section** — après la date du relevé `ok` précédent du même compte (sans borne s'il n'y
  en a pas) et jusqu'à `statement_date` incluse — met cette section **en erreur** (« période déjà couverte par la
  synchronisation bancaire »), comme une collision d'identité aujourd'hui. Seul ajout dans `importer/service.py`.
  Un PDF qui comble un trou antérieur à la première opération API reste donc importable. *(choix du concepteur)*
- **Trou d'historique** : si le dernier PDF d'un compte est plus ancien que ce que la banque fournit (souvent 90
  jours), la période manquante n'est récupérable que par les PDF. D'où la consigne : importer tous les relevés PDF
  disponibles **avant** de connecter la banque (§ 7). Le trou se verrait sinon dans le contrôle de solde.
- Si l'utilisateur supprime le dernier relevé PDF d'un compte (fonction de l'autre session), la frontière recule ;
  la synchro suivante comble alors ce que la banque fournit encore.
- **Date d'opération** retenue : `booking_date`, sinon `transaction_date`, sinon `value_date`. Une opération sans
  aucune date est écartée et comptée dans le rapport de synchro.
- Une opération passée en compte le jour même du relevé mais absente du PDF (ou l'inverse) se verrait dans le
  contrôle de solde (§ 5.8).

### 5.6 Rattachement des comptes

- Pour chaque compte renvoyé par l'API : si les chiffres d'un `accounts.number` existant (PDF) figurent dans les
  chiffres de l'IBAN et qu'**un seul** compte correspond, il est rattaché automatiquement.
- **Aucun candidat** : un nouveau compte est créé automatiquement (`number` = IBAN, `label` = `name` ou `product`,
  `kind` = `savings` si `cash_account_type` = SVGS sinon `checking`, `source` = `enablebanking`). L'utilisateur a
  déjà choisi les comptes partagés dans l'écran de consentement de la banque ; le créer tout de suite évite de
  perdre l'historique long, disponible seulement dans l'heure qui suit l'autorisation. Il peut ensuite le passer en
  « ignoré » (ses opérations déjà importées restent). *(choix du concepteur)*
- **Plusieurs candidats** (cas rare) : le compte apparaît « à rattacher » dans l'onglet Banque (un compte existant,
  nouveau compte, ou ignorer) et n'est pas synchronisé d'ici là ; l'onglet prévient que l'historique au-delà de
  90 jours peut être perdu si le choix tarde.
- Un rattachement peut être changé tant que le compte API n'a importé aucune opération ; ensuite il est figé
  (changer de compte déplacerait des opérations : hors périmètre).
- L'interface n'affiche que les 4 derniers caractères de l'IBAN, comme pour les numéros de compte.

### 5.7 Synchronisation

- **Planifiée dans le processus** : un thread démon lancé au démarrage de l'appli (pas dans les tests) synchronise
  tous les liens actifs toutes les `EXTRANALYZER_SYNC_HOURS` heures (12 par défaut : 2 consultations par jour,
  sous la limite courante de 4). Première passe quelques minutes après le démarrage.
- **Manuelle** : bouton « Synchroniser maintenant » dans l'onglet Banque.
- **Fenêtre** : `date_from` = le plus tardif de (frontière + 1 jour) et (dernière opération API du compte − 10 jours)
  pour rattraper les opérations comptabilisées en retard ; `strategy=longest` à la première synchro d'un compte,
  `default` ensuite ; pagination par `continuation_key` jusqu'à épuisement. Seules les opérations `status = BOOK`
  sont gardées (filtre côté client, en plus de `transaction_status=BOOK` si la banque l'accepte).
- **Déroulé** : tous les appels réseau d'abord, **hors** du verrou d'écriture ; puis une seule transaction SQLite
  par synchro (insertions, contrôles de solde, une passe de classement). En cas d'erreur réseau ou de réponse
  invalide sur un compte, ce compte n'écrit rien et l'erreur est enregistrée ; les autres comptes passent.
- **Erreurs** : 401 `EXPIRED_SESSION` (ou session non `AUTHORIZED`) → lien `expired`, alerte ; 429 → plus de
  tentative planifiée avant 6 h ; autres erreurs → message enregistré, nouvelle tentative au tour suivant.
- Chaque synchro est journalisée (`sync_runs` : début, fin, déclencheur, nombre d'opérations ajoutées, ignorées
  avant la frontière, en erreur, message). Les logs ne contiennent ni jeton, ni identifiant de session, ni IBAN.

### 5.8 Contrôle de solde (remplace la réconciliation des sections)

- Les opérations API n'ont **pas de section** (`section_id` NULL) ; aucune section ni document n'est créé pour elles.
- À chaque synchro d'un compte rattaché ayant un relevé PDF : solde attendu = clôture du dernier relevé PDF +
  Σ opérations API du compte jusqu'à la `reference_date` du solde bancaire. Il est comparé au solde comptable de la
  banque (premier disponible parmi CLBD, ITBD ; les soldes « disponibles » incluent les opérations en attente et ne
  servent pas au contrôle).
- Écart non nul → **avertissement** dans l'onglet Banque (montant de l'écart, date) ; l'import n'est pas bloqué.
  Pas de solde comptable ou pas de relevé PDF → « contrôle impossible », affiché comme tel.
- Un écart expliqué (trou d'historique, opération écartée pour devise étrangère) peut être **accepté** d'un clic :
  son montant est mémorisé par compte (`external_accounts.accepted_gap_cents`) et retranché des contrôles suivants ;
  tout nouvel écart réapparaît. *(choix du concepteur)*
- Le contrôle sert aussi à vérifier, à la première synchro, que la date d'opération des PDF et le `booking_date` de
  l'API coïncident autour de la frontière : une opération perdue ou doublée à la frontière ferait un écart.

### 5.9 Identité et stockage des opérations API

- `transactions.source` (nouvelle colonne, `'pdf'` par défaut) vaut `'enablebanking'`.
- **`tx_key`** : la clé de contenu actuelle, préfixée de la source :
  `sha1("enablebanking|<numéro du compte>|<date>|<montant>|<libellé normalisé>|<rang>")`, où le rang compte les
  doublons exacts (même date, montant et libellé normalisé) **dans la réponse**. La fenêtre de synchro commence
  toujours à un jour entier, donc chaque jour récupéré l'est en entier et le rang ne dépend pas de l'ordre de la
  réponse. Rien ne garantit dans la doc que `transaction_id` reste stable d'une session à l'autre (un renouvellement
  crée une nouvelle session) : il n'entre donc pas dans l'identité, il est seulement conservé dans le JSON brut.
  L'unicité de `tx_key` garantit qu'une resynchro ou un renouvellement n'insère rien deux fois ; une opération déjà
  présente n'est pas modifiée. *(choix du concepteur)*
- Limite connue : si la banque modifie le libellé d'une opération déjà comptabilisée entre deux synchros, elle
  serait insérée deux fois ; le contrôle de solde le révélerait.
- **Libellé** : `remittance_information` joint par des espaces ; à défaut le nom du créancier ou du débiteur, puis
  la description de `bank_transaction_code`. Le format peut différer des libellés PDF : les règles et le modèle
  appris sur les PDF pourraient moins bien reconnaître ces lignes au début (voir § 7).
- **Montant** : `transaction_amount.amount` (valeur absolue) signé par `credit_debit_indicator` (DBIT → négatif).
  Une devise autre que EUR est écartée et comptée en erreur.
- La réponse brute de chaque opération est gardée (`external_transactions.raw_json`) pour pouvoir recalculer les
  libellés si la règle de composition change.
- Les reclassements manuels fonctionnent comme aujourd'hui (`manual_overrides` par `tx_key`).

### 5.10 Classement et appariement

- Après les insertions d'une synchro, `on_new_transactions` (donc `reclassify`) est appelé **une fois**, comme
  pour un import PDF. Aucune modification de `app/classify/`.
- L'appariement des virements internes prend déjà les lignes sans section ; un virement courant → livret apparaît
  côté API des deux comptes et est apparié normalement. Un virement dont une jambe est en PDF (avant la frontière
  d'un compte) et l'autre en API reste appariable (fenêtre de 5 jours).

### 5.11 Alertes

- `/api/meta` gagne un champ `bank` : `null` si la fonctionnalité est désactivée ou sans lien, sinon
  `{ "status": "ok" | "expiring" | "expired" | "error", "days_left": n, "last_sync": "…" }`.
- `expiring` à **14 jours** ou moins de `valid_until`. La barre commune affiche une pastille « Banque : renouveler
  (n j) » / « Banque : connexion expirée » / « Banque : erreur de synchro », qui ouvre l'onglet Banque.

### 5.12 Schéma (migration 5 → 6)

```sql
ALTER TABLE transactions ADD COLUMN source TEXT NOT NULL DEFAULT 'pdf';
CREATE TABLE bank_links (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT UNIQUE NOT NULL,
    aspsp_name TEXT NOT NULL, aspsp_country TEXT NOT NULL, valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','expired','replaced','revoked')),
    created_at TEXT NOT NULL, last_error TEXT, retry_after TEXT);
CREATE TABLE bank_auth_states (state TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE external_accounts (id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES bank_links(id),
    uid TEXT NOT NULL, identification_hash TEXT, iban TEXT, name TEXT, currency TEXT, cash_account_type TEXT,
    account_id INTEGER NULL REFERENCES accounts(id), ignored INTEGER NOT NULL DEFAULT 0,
    last_sync_at TEXT, balance_cents INTEGER, balance_date TEXT, balance_gap_cents INTEGER,
    accepted_gap_cents INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE external_transactions (transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
    raw_json TEXT NOT NULL);
CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, trigger TEXT NOT NULL,
    added INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    message TEXT);
```

Si la session « suppression des relevés » change aussi la version du schéma, les migrations sont renumérotées au
rebasage.

### 5.13 Interface : onglet « Banque »

```
Banque                                         [Synchroniser maintenant]
Crédit Mutuel — consentement valable jusqu'au 11/05/2027 (180 j)   [Renouveler] [Déconnecter]
Dernière synchro : 12/11 08:00 — 4 opérations ajoutées

Comptes
  Compte courant  ••••1234  → Compte courant (PDF)   solde vérifié ✓
  Livret Bleu     ••••5678  → Livret Bleu (PDF)      écart de 12,40 € au 10/11 ⚠
  Compte joint    ••••9012  → [à rattacher ▾]

Synchros récentes (10 dernières)
Retour de la banque : [coller l'adresse ici] [Valider]     (repli, § 5.4)
```

Sans configuration : un encart explique les variables et le secret à fournir.

### 5.14 Sécurité

- Routes `/api/bank/*` derrière l'authentification et la protection CSRF existantes ; `/bank/callback` est une page
  statique sans donnée, servie sans session.
- `state` aléatoire à usage unique, expiré après 30 minutes : un retour forgé ne peut pas rattacher la banque de
  quelqu'un d'autre.
- `session_id` en base (inutilisable sans la clé privée, qui reste un secret Docker hors base).
- Appels HTTPS avec vérification du certificat (comportement par défaut d'`urllib`), délai de 30 s.
- Réponses de l'API traitées comme non fiables : types vérifiés, montants parsés en `Decimal`, champs inconnus
  ignorés.

### 5.15 Tests

Aucun appel réseau : un faux transport rejoue des réponses JSON fictives.
- JWT : en-tête et charges attendus, signature vérifiée avec une clé RSA générée dans le test.
- Client : pagination par `continuation_key`, erreurs 401/429/5xx typées.
- Connexion : `state` inconnu, expiré, réutilisé refusés ; création du lien et des comptes ; renouvellement qui
  retrouve les rattachements.
- Rattachement automatique (un seul candidat), ambigu, nouveau compte, ignoré.
- Synchro : frontière respectée, `BOOK` seulement, signe, devise, identité stable et resynchro sans doublon,
  repli sans identifiant, échec d'un compte sans effet sur les autres, une seule passe de classement, appariement
  courant ↔ livret, contrôle de solde (égal, écart, impossible).
- Import PDF refusé quand la période est déjà couverte par l'API.
- `/api/meta` : alerte d'expiration à 14 jours, expirée, erreur.
- Planificateur : la fonction d'un tour est testée directement (pas d'attente réelle).

## 6. Laissé au builder

- Découpage exact des modules et noms des fonctions.
- Transmission éventuelle des en-têtes PSU lors d'une synchro manuelle.
- Délai de la première synchro planifiée après démarrage, gestion de l'arrêt du thread.
- Présentation exacte de l'onglet et de la pastille.

## 7. Points à vérifier par l'utilisateur lors de la vraie connexion

- **Avant de connecter** : importer tous les relevés PDF disponibles, jusqu'au plus récent, pour que la frontière
  soit la plus tardive possible (§ 5.5).
- Après la première synchro : regarder le contrôle de solde de chaque compte.

- Le Control Panel accepte-t-il `http://10.10.10.95:8000/bank/callback` ? Sinon, utiliser le repli par copier-coller.
- Coût réel du mode restreint.
- Nom exact de la banque renvoyé par `/aspsps` et prise en charge des comptes de particuliers.
- Forme des libellés API : après la première synchro, vérifier dans « À classer » si les règles existantes les
  reconnaissent ; sinon ajuster les règles (ou la composition du libellé, recalculable grâce au JSON brut gardé).
