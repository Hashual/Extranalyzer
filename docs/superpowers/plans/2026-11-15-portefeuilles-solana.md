# Portefeuilles Solana — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** une section « Portefeuilles Solana » dans l'onglet Banque. Elle lit plusieurs adresses publiques via
Helius (SOL, jetons dont xStocks, staking natif), les valorise en euros, garde l'historique de la valeur et relie
chaque portefeuille à une sous-catégorie « Épargne / <nom> ».

**Architecture :** une nouvelle source `app/sources/solana/` :
- un client HTTP à transport injectable ;
- une lecture pure qui produit des positions ;
- un stockage par relevé (snapshot) ;
- un runtime avec verrou et `Scheduler`.

Elle est indépendante de `BankRuntime`. Les routes de `app/tabs/banque_wallets.py` sont incluses dans le routeur
de l'onglet Banque. Le rendu client est délégué par `banque.js` à `banque-wallets.js`.

**Tech Stack :** Python 3.12, FastAPI, SQLite (`sqlite3`), `urllib`, JS vanilla en modules ES, Chart.js embarqué.

**Spec :** `docs/superpowers/specs/2026-11-15-portefeuilles-solana-design.md`

## Global Constraints

- Aucune nouvelle dépendance Python. HTTP en `urllib`, comme `app/sources/enablebanking/client.py`.
- Les tests n'appellent jamais le réseau. Ils n'utilisent que des adresses et des montants **fictifs**.
- La clé Helius n'apparaît **jamais** :
  - ni dans la base, ni dans les logs, ni dans les réponses d'API, ni dans les messages d'erreur ;
  - or l'URL contient la clé en paramètre (`?api-key=`) : aucun message ne contient donc l'URL.
- Les adresses ne sont jamais journalisées.
- Pas d'écriture sur la blockchain, pas de clé privée.
- Valeurs en centimes d'euro (`int`). Quantités et prix en `Decimal`, stockés en `TEXT`.
- Un jeton sans prix n'est jamais compté pour zéro en silence : il est compté dans `unpriced_count`, et l'interface
  affiche « hors N jeton(s) sans prix ».
- Une actualisation en échec n'écrase pas le dernier relevé `ok`.
- Les valeurs des portefeuilles n'entrent pas dans `app/analytics/totals.py`.
- Variables : `EXTRANALYZER_HELIUS_KEY_FILE` (par défaut `/run/secrets/helius_key`), `EXTRANALYZER_HELIUS_URL`
  (par défaut `https://mainnet.helius-rpc.com`). La période reprend `EXTRANALYZER_SYNC_HOURS`.
- Commits en français, terminés par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Ne jamais pousser : un push sur `main` redéploie le Pi.
- Venv : `C:/Users/<utilisateur>/Documents/Perso/Applications/Extranalyzer/.venv`. Tests :
  `.venv/Scripts/python -m pytest` depuis le worktree. Le chemin du venv est absolu.

## Review Focus

1. **Quota Helius atteint (HTTP 429) ou Helius injoignable pendant l'actualisation.** Attendu : relevé `error`
   avec un message lisible sans URL, le dernier relevé `ok` reste affiché, les autres portefeuilles sont
   actualisés. Testé en tâche 5.
2. **Adresse mal saisie** : espaces autour, adresse Ethereum `0x…`, base58 de mauvaise longueur, adresse déjà
   suivie. Attendu : espaces retirés, refus 422 ou 409 avec un message clair. Testé en tâches 4 et 6.
3. **Portefeuille vide ou fermé** : aucun jeton, aucun staking, solde nul. Attendu : relevé `ok` à 0 €, pas
   d'erreur. Testé en tâche 3.
4. **Compte de staking en cours de désactivation ou déjà désactivé** mais pas encore retiré. Attendu : il reste
   compté, avec son état affiché. Testé en tâche 3.
5. **Deux actualisations simultanées** (bouton + synchro planifiée). Attendu : la seconde reçoit 409 « déjà en
   cours », aucune double écriture. Testé en tâche 5.

---

### Task 1 : Schéma v7

**Files :**
- Modify : `app/store/schema.sql` (tables avant la ligne `PRAGMA user_version`, passer à 7)
- Modify : `app/store/db.py` (`_MIGRATE_6_TO_7`, `SCHEMA_VERSION = 7`, branche `if version == 6`)
- Test : `tests/test_store.py`

**Interfaces :**
- Produces : les tables ci-dessous, que lisent les tâches 4 à 6.

```sql
CREATE TABLE wallets (
    id INTEGER PRIMARY KEY,
    address TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    savings_category_id INTEGER NULL REFERENCES categories(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_error TEXT
);
CREATE TABLE wallet_snapshots (
    id INTEGER PRIMARY KEY,
    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    taken_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','error')),
    total_eur_cents INTEGER,
    unpriced_count INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE TABLE wallet_positions (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES wallet_snapshots(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('sol','token','stake')),
    mint TEXT, symbol TEXT, name TEXT,
    amount TEXT NOT NULL, decimals INTEGER,
    price_eur TEXT, price_source TEXT, value_eur_cents INTEGER,
    stake_account TEXT, vote_account TEXT,
    stake_state TEXT CHECK(stake_state IN ('activating','active','deactivating','inactive'))
);
CREATE TABLE validator_labels (vote_account TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE hidden_mints (mint TEXT PRIMARY KEY);
CREATE INDEX idx_wallet_snapshots_wallet ON wallet_snapshots(wallet_id, taken_at);
CREATE INDEX idx_wallet_positions_snapshot ON wallet_positions(snapshot_id);
```

- [ ] **Step 1 : Tests en échec**
  - `test_fresh_schema_has_wallet_tables` : une base neuve a `user_version == 7` et les 5 tables.
  - `test_migrate_6_to_7` : une base créée avec le schéma v6 (reprendre le procédé des tests de migration
    existants de `tests/test_store.py`) est migrée à 7, ses données sont conservées, les tables existent.
  - `test_wallet_delete_cascades` : supprimer un `wallets` supprime ses snapshots et positions (`foreign_keys=ON`).
- [ ] **Step 2 :** `.venv/Scripts/python -m pytest tests/test_store.py -v` → échec attendu (tables absentes).
- [ ] **Step 3 :** écrire la migration et compléter `schema.sql`. Le texte SQL doit être le même dans les deux
  fichiers.
- [ ] **Step 4 :** relancer → PASS. Lancer toute la suite → PASS.
- [ ] **Step 5 : Commit** `feat: schéma v7 des portefeuilles Solana`

---

### Task 2 : Client Helius

**Files :**
- Create : `app/sources/solana/__init__.py`, `app/sources/solana/client.py`
- Create : `tests/fakehelius.py` (faux transport JSON-RPC)
- Test : `tests/test_solana_client.py`

**Interfaces :**
- Consumes : le type `Transport` et `urllib_transport` de `app/sources/enablebanking/client.py`. Les importer, sans
  les recopier. Attention : `urllib_transport` lève `EnableBankingError(0, "NETWORK", …)`. Le client Solana
  l'attrape et la convertit en `HeliusError(0, "NETWORK", "Helius injoignable")`.
- Produces :
  - `class HeliusError(Exception)`, avec les attributs `status: int`, `code: str` et `message: str` ;
  - `class RateLimited(HeliusError)` pour le statut 429 ;
  - `class HeliusClient(api_key: str, base_url: str = "https://mainnet.helius-rpc.com", transport: Transport = urllib_transport)`,
    avec les méthodes suivantes :

    | Méthode | Rôle |
    |---|---|
    | `assets_by_owner(owner: str) -> tuple[list[dict], dict \| None]` | `(items DAS fongibles et non fongibles, nativeBalance)`, toutes les pages |
    | `asset(mint: str) -> dict` | DAS `getAsset` |
    | `stake_accounts(owner: str) -> list[dict]` | comptes Stake en `jsonParsed`, dédoublonnés par `pubkey` |
    | `epoch() -> int` | `getEpochInfo().epoch` |

  - `tests/fakehelius.py` fournit `FakeHelius`, un appelable compatible `Transport`. Il répond selon le champ
    `method` du corps JSON-RPC : `fake.on("getAssetsByOwner", reponse1, reponse2…)`. Il garde ses requêtes dans
    `fake.requests`, comme `tests/fakebank.py`.

Détails :
- Toutes les requêtes partent en `POST {base_url}/?api-key={clé}`, avec un corps JSON-RPC 2.0.
- `assets_by_owner` :
  - paramètres `{"ownerAddress", "page", "limit": 1000, "displayOptions": {"showFungible": true, "showNativeBalance": true}}` ;
  - si la documentation en vigueur attend `options` à la place de `displayOptions`, suivre la documentation ;
  - pagination tant qu'une page contient `limit` éléments, avec une limite de 50 pages.
- `stake_accounts` :
  - deux appels `getProgramAccounts` sur `Stake11111111111111111111111111111111111111` ;
  - encodage `jsonParsed` ;
  - filtres `memcmp` à l'offset 12 (autorité de staking) puis 44 (autorité de retrait), `bytes = owner` ;
  - vérifier ces offsets dans la documentation du programme Stake.
- Erreurs :
  - un statut HTTP autre que 200, ou un champ `error` JSON-RPC, lève `HeliusError`, avec un message tronqué à
    200 caractères ;
  - un statut 429 lève `RateLimited` ;
  - délai de 30 s, celui du transport ;
  - aucun message ne contient `api-key` ni la clé.

- [ ] **Step 1 : Tests en échec**
  - `test_assets_paginates` : deux pages (1000 puis 3 éléments) donnent 1003 éléments. Les requêtes ont
    `page = 1` puis `page = 2`.
  - `test_assets_native_balance` : le `nativeBalance` de la première page est renvoyé.
  - `test_stake_accounts_dedup` : le même `pubkey` renvoyé par les deux filtres n'apparaît qu'une fois. Les deux
    requêtes portent les offsets 12 et 44.
  - `test_rate_limited` : un statut 429 lève `RateLimited`.
  - `test_rpc_error` : `{"error": {"code": -32602, "message": "x"}}` lève `HeliusError`.
  - `test_network_error` : un transport qui lève `EnableBankingError(0, "NETWORK", …)` donne
    `HeliusError(code="NETWORK")`.
  - `test_errors_never_leak_key` : pour chacun des cas d'erreur ci-dessus avec la clé `"cle-secrete-fictive"`,
    `"cle-secrete-fictive" not in str(exc)` et `"api-key" not in str(exc)`.
- [ ] **Step 2 :** `pytest tests/test_solana_client.py -v` → échec (module absent).
- [ ] **Step 3 :** implémenter `client.py` et `fakehelius.py`.
- [ ] **Step 4 :** relancer → PASS.
- [ ] **Step 5 : Commit** `feat: client Helius (DAS, comptes de staking, époque)`

---

### Task 3 : Lecture d'un portefeuille en positions valorisées

**Files :**
- Create : `app/sources/solana/holdings.py`
- Test : `tests/test_solana_holdings.py`

**Interfaces :**
- Consumes : `HeliusClient` (tâche 2).
- Produces :

```python
@dataclass(frozen=True)
class Position:
    kind: str                     # 'sol' | 'token' | 'stake'
    amount: Decimal               # en unités (SOL, jetons), pas en lamports
    mint: str | None = None
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    price_eur: Decimal | None = None
    price_source: str | None = None      # 'helius'
    value_eur_cents: int | None = None
    stake_account: str | None = None
    vote_account: str | None = None
    stake_state: str | None = None

@dataclass(frozen=True)
class WalletReading:
    positions: list[Position]
    total_eur_cents: int          # somme des positions valorisées et non masquées
    unpriced_count: int           # positions sans prix et non masquées

class NoEurRate(Exception): ...

SOL_MINT = "So11111111111111111111111111111111111111112"
EURC_MINT = "HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr"   # vérifier sur la documentation de Circle
FUNGIBLE_INTERFACES = {"FungibleToken", "FungibleAsset"}

def stake_state(activation_epoch: int, deactivation_epoch: int, epoch: int) -> str: ...
def read_wallet(client: HeliusClient, address: str, hidden_mints: frozenset[str] = frozenset()) -> WalletReading: ...
```

Règles :
- **Jetons** : on garde les `items` dont l'`interface` appartient à `FUNGIBLE_INTERFACES`. Les autres (NFT) sont
  ignorés.
  - quantité = `token_info.balance / 10**decimals` ;
  - symbole et nom tirés de `token_info.symbol` ou `content.metadata` ;
  - prix USD = `token_info.price_info.price_per_token` s'il est présent.
- **SOL disponible** : `nativeBalance.lamports / 1e9`. Prix USD = `nativeBalance.price_per_sol`. Si ce prix
  manque, prendre le prix de `SOL_MINT` via `asset()`.
- **Staking** : pour chaque compte, lire `parsed.info.stake.delegation` (`voter`, `activationEpoch`,
  `deactivationEpoch`).
  - `amount = account.lamports / 1e9`. Le montant compte tout le compte, récompenses comprises.
  - Le prix est celui du SOL.
  - Un compte `initialized` sans délégation devient une position `stake` avec `stake_state='inactive'` et sans
    `vote_account`.
- **`stake_state`** (époques au format u64 ; `deactivation == 2**64-1` signifie « jamais ») :

  | Condition | État |
  |---|---|
  | `activation >= epoch` | `activating` |
  | sinon, `deactivation == 2**64-1` | `active` |
  | sinon, `deactivation >= epoch` | `deactivating` |
  | sinon | `inactive` |

- **Taux EUR** : `eur_per_usd = 1 / prix_usd(EURC)`. On prend le prix de l'EURC dans les items s'il y figure,
  sinon par `client.asset(EURC_MINT)`. Sans prix EURC, on lève `NoEurRate`.
- **Valorisation** :
  - `price_eur = price_usd * eur_per_usd` ;
  - `value_eur_cents = round(amount * price_eur * 100)` en `ROUND_HALF_EVEN`, ce qui suit `app/importer/amounts.py` ;
  - une quantité nulle (jeton à 0) est écartée.
- **Masquage** : une position dont le `mint` est dans `hidden_mints` reste dans `positions`, mais n'entre ni dans
  `total_eur_cents` ni dans `unpriced_count`.

- [ ] **Step 1 : Tests en échec.** Valeurs fictives : SOL = 150 $, EURC = 1,08 $, donc 1 $ ≈ 0,9259 €.
  - `test_reads_sol_tokens_and_stake` :
    - données : 2 SOL, un jeton USDC à 1,00 $ (100 unités), un xStock Token-2022 à 300 $ (0,5 unité), un compte
      de staking de 10 SOL actif ;
    - attendu : 4 positions ;
    - `total_eur_cents == round((2*150 + 100*1 + 0.5*300 + 10*150) / 1.08 * 100)` ;
    - `unpriced_count == 0`.
  - `test_nft_ignored` : un item `V1_NFT` est absent des positions.
  - `test_token_without_price` : la position a `price_eur is None` et `value_eur_cents is None`, et
    `unpriced_count == 1`.
  - `test_hidden_mint_excluded_from_totals` : la position est présente, mais n'est pas comptée dans les totaux.
  - `test_empty_wallet` : `nativeBalance` à 0, aucun item, aucun compte de staking. Attendu : positions SOL vides
    ou SOL à 0 écartées, `total_eur_cents == 0`, sans exception.
  - `test_stake_states` : les quatre états, plus `deactivating` et `inactive` qui restent comptés en valeur.
  - `test_no_eurc_price_raises` : sans prix EURC, `NoEurRate` est levée.
- [ ] **Step 2 :** `pytest tests/test_solana_holdings.py -v` → échec.
- [ ] **Step 3 :** implémenter `holdings.py`.
- [ ] **Step 4 :** relancer → PASS.
- [ ] **Step 5 : Commit** `feat: lecture et valorisation en euros d'un portefeuille Solana`

---

### Task 4 : Portefeuilles en base et lien avec l'épargne

**Files :**
- Modify : `app/classify/savings_target.py` (ajouter `savings_category_named`)
- Create : `app/sources/solana/wallets.py`
- Test : `tests/test_solana_wallets.py`

**Interfaces :**
- Consumes : `Position`, `WalletReading` (tâche 3) ; `_savings_root` de `savings_target.py`.
- Produces :

```python
# app/classify/savings_target.py
def savings_category_named(conn, name: str) -> int:   # sous-catégorie d'épargne `name`, retrouvée (fold) ou créée

# app/sources/solana/wallets.py
class WalletError(ValueError): ...           # message destiné à l'utilisateur
class DuplicateWallet(WalletError): ...
def clean_address(raw: str) -> str            # strip ; base58 valide décodé en 32 octets, sinon WalletError
def add_wallet(conn, label: str, address: str, now: str) -> int
def rename_wallet(conn, wallet_id: int, label: str) -> None
def remove_wallet(conn, wallet_id: int) -> None
def set_validator_label(conn, vote_account: str, label: str | None) -> None   # None ou "" : supprime
def hide_mint(conn, mint: str, hidden: bool) -> None
def hidden_mints(conn) -> frozenset[str]
def record_reading(conn, wallet_id: int, taken_at: str, reading: WalletReading) -> int
def record_error(conn, wallet_id: int, taken_at: str, message: str) -> int
def overview(conn) -> dict
```

Règles :
- `clean_address` : décodage base58 écrit à la main (alphabet Bitcoin), sans dépendance. Il faut exactement
  32 octets. Message : « adresse Solana invalide ».
- `add_wallet` :
  - le libellé est requis, avec `strip` et 60 caractères au plus ;
  - une adresse déjà suivie lève `DuplicateWallet` (« adresse déjà suivie ») ;
  - `savings_category_id = savings_category_named(conn, label)`.
- `rename_wallet` : si la catégorie liée existe encore et que son nom est égal (`fold`) à l'ancien libellé, la
  renommer. Si une sœur porte déjà le nouveau nom, ne pas renommer et ne rien lever. Sinon, la laisser.
- `remove_wallet` : supprime le portefeuille, ses snapshots et ses positions (cascade). La catégorie et les
  transactions sont conservées.
- `record_reading` : un snapshot `ok` avec ses positions, et `wallets.last_sync_at = taken_at`,
  `last_error = NULL`.
- `record_error` : un snapshot `error` avec un message de 200 caractères au plus, et `last_error = message`. Le
  dernier snapshot `ok` n'est pas touché.
- `overview(conn)` renvoie :

  ```
  {total_eur, unpriced_count, last_refresh,
   wallets: [{id, label, address, category_id, category_name, net_deposited, deposits, withdrawals,
              total_eur, unpriced_count, last_sync_at, last_error,
              positions: [...], history: [{taken_at, total_eur}]}],
   validators: {vote: label}, hidden_mints: [...]}
  ```

  - Montants en `Decimal` d'euros, via `from_cents`.
  - Les positions et les totaux viennent du **dernier snapshot `ok`**.
  - Le total courant est **recalculé** en excluant les `hidden_mints` actuels.
  - L'historique contient les snapshots `ok`, par date croissante.
  - Chaque position porte `hidden: bool` et `validator_label`.
- **Net versé** (spec § 5.6), calculé sur la catégorie liée et **sur tous les comptes** :

  ```sql
  SELECT COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0),
         COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0)
  FROM transactions WHERE category_id = ? AND mirror_of IS NULL
  ```

  `net_deposited = deposits - withdrawals`. Si la catégorie est absente, les trois montants valent 0.

- [ ] **Step 1 : Tests en échec**
  - `test_clean_address` : une adresse fictive valide de 32 octets, entourée d'espaces, est acceptée.
    `0x52908400098527886E0F7030069857D2E4169EE7`, une chaîne base58 de 20 octets et une chaîne contenant `0OIl`
    lèvent `WalletError`.
  - `test_add_creates_savings_category` : `add_wallet(conn, "Principal", ADDR, now)` crée « Épargne / Principal ».
    Un second portefeuille nommé « principal » réutilise la même catégorie.
  - `test_add_duplicate` : la même adresse lève `DuplicateWallet`.
  - `test_rename_renames_category_when_unchanged` : la catégorie renommée suit. Si l'utilisateur avait déjà
    renommé la catégorie, elle n'est pas touchée.
  - `test_remove_keeps_category_and_transactions`.
  - `test_net_deposited` : des débits de 100 € (compte courant) et 50 € (livret) et un crédit de 30 €, classés dans
    la catégorie, donnent `deposits = 150`, `withdrawals = 30`, `net_deposited = 120`. Une jambe miroir est
    ignorée.
  - `test_error_keeps_last_ok` : après `record_reading` puis `record_error`, `overview` montre les positions du
    relevé `ok` et `last_error`.
  - `test_overview_recomputes_with_hidden_mints` : masquer un mint après le relevé fait baisser `total_eur`
    courant, mais pas l'historique.
- [ ] **Step 2 :** `pytest tests/test_solana_wallets.py -v` → échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** relancer, puis `pytest` complet → PASS. Les tests de `savings_target` existants doivent passer.
- [ ] **Step 5 : Commit** `feat: portefeuilles Solana en base, sous-catégorie d'épargne et net versé`

---

### Task 5 : Configuration, runtime et synchro planifiée

**Files :**
- Modify : `app/config.py` (`SolanaSettings`, `_solana_settings()`, champ `Settings.solana`)
- Create : `app/sources/solana/runtime.py`
- Modify : `app/main.py`
  - paramètre `solana_transport: Transport = urllib_transport` de `create_app` ;
  - `app.state.solana` ;
  - second `Scheduler` démarré et arrêté dans `lifespan`.
- Create : `docker-compose.solana.yml`
- Modify : `README.md` (section « Portefeuilles Solana (Helius) »)
- Test : `tests/test_solana_runtime.py`, `tests/test_config.py` s'il existe, sinon dans `tests/test_solana_runtime.py`

**Interfaces :**
- Consumes : tâches 2 à 4 ; `Scheduler` (`app/sources/scheduler.py`).
- Produces :

```python
# app/config.py
@dataclass(frozen=True)
class SolanaSettings:
    api_key: str = field(repr=False)
    base_url: str = "https://mainnet.helius-rpc.com"
# Settings.solana: SolanaSettings | None = None

# app/sources/solana/runtime.py
class NotConfigured(RuntimeError): ...
class RefreshBusy(RuntimeError): ...
@dataclass
class RefreshSummary:
    refreshed: int
    errors: int
class SolanaRuntime:
    def __init__(self, settings: SolanaSettings | None, read: Runner, write: Runner,
                 transport: Transport = urllib_transport, clock: Callable[[], datetime] = _utc_now): ...
    configured: bool                                     # propriété
    def write(self, fn): ...                             # comme BankRuntime.write
    def refresh(self, trigger: str, wallet_id: int | None = None) -> RefreshSummary: ...
    def scheduled_tick(self) -> None: ...                # n'échoue jamais, comme BankRuntime.scheduled_tick
```

Règles :
- `_solana_settings()` : lit le fichier `EXTRANALYZER_HELIUS_KEY_FILE`, par défaut `/run/secrets/helius_key`.
  - Fichier absent, illisible ou vide : renvoie `None`, avec un `log.warning` qui donne le type d'erreur seul.
  - L'URL vient de `EXTRANALYZER_HELIUS_URL`. Elle doit commencer par `https://`, sinon `None` et un warning.
- `refresh` :
  - Prend un verrou non bloquant, sinon lève `RefreshBusy`.
  - Lit d'abord, sous `read`, les portefeuilles visés (tous, ou `wallet_id`) et les `hidden_mints`.
  - Appelle ensuite `read_wallet` pour chacun, **hors verrou d'écriture**. Une `HeliusError` ou une `NoEurRate`
    est attrapée **par portefeuille**. `NoEurRate` donne le message « taux euro indisponible (prix de l'EURC
    absent) ».
  - Écrit enfin, sous `write`, une transaction par portefeuille, avec `record_reading` ou `record_error` et
    `commit`.
  - Horodatage ISO UTC de `clock()`.
- Le `Scheduler` n'est créé que si `solana.configured and settings.sync_hours > 0`. Le nom du thread passe en
  paramètre : ajouter `name: str = "synchro-bancaire"` à `Scheduler.__init__` et passer `"synchro-solana"`.
- `docker-compose.solana.yml` suit le modèle de `docker-compose.banque.yml` :
  - secret `helius_key` (`file: ./secrets/helius.key`) ;
  - `EXTRANALYZER_HELIUS_KEY_FILE: /run/secrets/helius_key` ;
  - `EXTRANALYZER_HELIUS_URL: ${HELIUS_URL:-https://mainnet.helius-rpc.com}`.
- README : créer la clé gratuite sur helius.dev, poser `secrets/helius.key` (`chown 10001:10001`, `chmod 400`),
  ajouter `docker-compose.solana.yml` à `COMPOSE_FILE` dans `.env`, puis `docker compose up -d`.

- [ ] **Step 1 : Tests en échec**
  - `test_settings_absent_key` : la variable pointe vers un fichier inexistant, donc `solana is None` et l'appli
    démarre.
  - `test_settings_rejects_http_url`.
  - `test_refresh_one_failure_does_not_block_others` :
    - données : 2 portefeuilles, `FakeHelius` en erreur 500 pour l'adresse A et OK pour B ;
    - attendu : `RefreshSummary(refreshed=1, errors=1)`, B a un snapshot `ok`, A garde son ancien `ok` et a
      `last_error`.
  - `test_refresh_rate_limited_message` : avec un statut 429, `last_error` est lisible et ne contient ni la clé ni
    `api-key`.
  - `test_refresh_busy` : verrou déjà pris (appel concurrent simulé en tenant le verrou), donc `RefreshBusy`.
  - `test_scheduled_tick_never_raises` : le transport lève une exception, `scheduled_tick()` rend la main.
  - `test_secret_never_in_repr` : `"cle" not in repr(SolanaSettings("cle-secrete-fictive"))`.
- [ ] **Step 2 :** `pytest tests/test_solana_runtime.py -v` → échec.
- [ ] **Step 3 :** implémenter la configuration, le runtime, le câblage dans `main.py`, le compose et le README.
- [ ] **Step 4 :** `pytest` complet → PASS. Les tests de `Scheduler` et du démarrage existants doivent passer.
- [ ] **Step 5 : Commit** `feat: configuration Helius, actualisation des portefeuilles et synchro planifiée`

---

### Task 6 : Routes de l'API (section de l'onglet Banque)

**Files :**
- Create : `app/tabs/banque_wallets.py` (`router = APIRouter()`)
- Modify : `app/tabs/banque.py` (`router.include_router(banque_wallets.router, prefix="/wallets")`)
- Test : `tests/test_banque_wallets_api.py`

**Interfaces :**
- Consumes : `request.app.state.solana` (`SolanaRuntime`), `wallets.py` (tâche 4), `get_conn`.
- Produces, toutes les routes sous `/api/tabs/banque/wallets` :

| Méthode et chemin | Corps | Réponse | Erreurs |
|---|---|---|---|
| `GET ""` | | `{"configured": bool, **overview(conn)}` via `to_jsonable` | |
| `POST ""` | `{label, address}` | `{"id": int, "refresh": RefreshSummary \| null}` : actualisation immédiate du nouveau portefeuille si configuré | 422 `WalletError`, 409 `DuplicateWallet` |
| `PATCH "/{wallet_id}"` | `{label}` | `{"ok": true}` | 404, 422 |
| `DELETE "/{wallet_id}"` | | `{"ok": true}` | 404 |
| `POST "/refresh"` | `{wallet_id?: int}` | `RefreshSummary` | 409 « une actualisation est déjà en cours », 503 « portefeuilles non configurés » |
| `PUT "/validators/{vote_account}"` | `{label}` | `{"ok": true}` | 422 si l'adresse est invalide |
| `PUT "/hidden-mints/{mint}"` | `{hidden: bool}` | `{"ok": true}` | 422 si le mint est invalide |

Les écritures passent par `runtime.write`, avec commit, comme `_commit` dans `banque.py`.

- [ ] **Step 1 : Tests en échec.** Fixtures sur le modèle de `tests/test_banque_api.py` :
  `create_app(settings, solana_transport=FakeHelius(...))`, `Settings.solana = SolanaSettings("cle-secrete-fictive")`.
  - `test_get_not_configured` : sans `solana`, `configured: false` et `wallets: []`. L'onglet Banque répond
    toujours.
  - `test_add_refreshes_and_lists` : après le POST, le GET montre le portefeuille, ses positions et son
    `category_name == "Principal"`.
  - `test_add_invalid_address_422` (adresse Ethereum) et `test_add_strips_spaces`.
  - `test_add_duplicate_409`.
  - `test_rename_and_delete`, puis `test_delete_unknown_404`.
  - `test_refresh_busy_409` et `test_refresh_not_configured_503`.
  - `test_validator_label_and_hidden_mint` : le libellé apparaît dans `validators`, et le mint masqué est exclu de
    `total_eur`.
  - `test_key_never_in_responses` : pour chaque réponse ci-dessus, y compris d'erreur, `"cle-secrete-fictive"` et
    `"api-key"` sont absents de `response.text`.
  - `test_csrf_required` : un POST sans `X-Extranalyzer` reçoit 403.
- [ ] **Step 2 :** `pytest tests/test_banque_wallets_api.py -v` → échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** `pytest` complet → PASS.
- [ ] **Step 5 : Commit** `feat: routes des portefeuilles Solana dans l'onglet Banque`

---

### Task 7 : Interface « Portefeuilles Solana »

**Files :**
- Create : `app/web/tabs/banque-wallets.js` (`export async function walletsSection(ctx): Promise<HTMLElement>`)
- Modify : `app/web/tabs/banque.js`
  - insérer la section entre les cartes des banques et `runsCard(data)` ;
  - l'afficher aussi quand la banque n'est **pas** configurée, au lieu du retour anticipé qui ne montre que
    `notConfigured()`.
- Modify : `app/web/style.css` (quelques classes seulement, en suivant les variables de thème existantes)

**Interfaces :**
- Consumes : routes de la tâche 6. Dans `ui.js` : `el`, `section`, `money`, `emptyNote`. Dans `charts.js` :
  `mountChart`, `euroTooltip`.

Contenu (esquisse de la spec § 5.2) :
- **En-tête** : total général, mention « hors N jeton(s) sans prix » si N > 0, dernière actualisation, bouton
  « Actualiser ».
- **Une carte par portefeuille** :
  - nom, adresse raccourcie (`4 premiers…4 derniers`) avec un lien vers `https://solscan.io/account/<adresse>`
    (`rel="noreferrer"`, `target="_blank"`) ;
  - total, et « Net versé depuis la banque : X € (Épargne / <catégorie>) » ;
  - `last_error` en alerte s'il y en a une ;
  - tableau des positions : SOL disponible, puis staking (libellé du validateur ou compte de vote raccourci,
    bouton « Nommer », état en français : `activating` « en activation », `active` « actif », `deactivating` « en
    désactivation », `inactive` « inactif »), puis jetons triés par valeur ;
  - jetons sans prix dans un `<details>` « N jeton(s) sans prix » ; jetons masqués dans un `<details>`
    « Masqués », avec un bouton « Masquer » / « Réafficher » ;
  - courbe Chart.js de `history` quand il y a au moins 2 points ;
  - boutons « Renommer » (`prompt`) et « Retirer » (`confirm` qui précise que la catégorie d'épargne et les
    virements sont conservés).
- **Formulaire d'ajout** : nom et adresse.
- **Non configuré** : texte de mise en route qui renvoie à la section du README, avec la liste des portefeuilles
  déjà saisis s'il y en a.
- Tous les montants en euros passent par `money(value, "balance")`, ce qui les soumet au masquage.

- [ ] **Step 1 :** implémenter.
- [ ] **Step 2 : Vérification dans l'appli** (skill `run`) :
  - démarrer l'appli localement avec un faux transport, ou sans clé (écran « non configuré » et ajout) ;
  - vérifier l'onglet Banque en largeur bureau et en largeur téléphone, en thème clair et sombre ;
  - vérifier le masquage des montants ;
  - console sans erreur.
- [ ] **Step 3 :** `pytest` complet → PASS. `tests/test_web_core.py` vérifie les fichiers statiques.
- [ ] **Step 4 : Commit** `feat: section Portefeuilles Solana dans l'onglet Banque`

---

### Task 8 : Vérification réelle des prix (avec l'utilisateur)

Elle demande la clé Helius de l'utilisateur et ses adresses réelles. Aucune n'est écrite dans le dépôt, les logs
ou la mémoire de l'assistant.

- [ ] **Step 1 :** l'utilisateur crée la clé, la pose en secret et ajoute une adresse qui contient des xStocks et
  du staking.
- [ ] **Step 2 :** vérifier dans l'interface que :
  - chaque xStock a un prix ;
  - les comptes de staking apparaissent ;
  - le total est cohérent avec ce que l'utilisateur voit sur Solscan ou dans son portefeuille.
- [ ] **Step 3 :** si des xStocks restent sans prix, **arrêter** et proposer un complément Jupiter Price v3
  (spec § 5.4, point 5) dans un plan séparé. Rien n'est construit sans l'accord de l'utilisateur.
