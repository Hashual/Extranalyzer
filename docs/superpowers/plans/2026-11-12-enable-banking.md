# Enable Banking — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** récupérer automatiquement les opérations des comptes Crédit Mutuel via l'API Enable Banking, à partir du
dernier relevé PDF de chaque compte, classées par la chaîne existante.

**Architecture :** une couche générique `app/sources/` (objets récupérés, frontière PDF/API, insertion, contrôle de
solde) et un fournisseur `app/sources/enablebanking/` (JWT, client HTTP à transport injectable, conversion, connexion
et synchro). Un onglet « Banque » et un thread de synchro planifiée branchent le tout.

**Tech Stack :** Python 3.12, FastAPI, SQLite, `cryptography` (déjà présent) pour RS256, `urllib.request`.

**Spec :** `docs/superpowers/specs/2026-11-12-enable-banking-design.md`

## Global Constraints

- Aucune nouvelle dépendance d'exécution (`constraints.txt` inchangé).
- Aucun appel réseau dans les tests : transport simulé.
- Jamais de jeton, `session_id`, clé ou IBAN complet dans les logs ou l'interface (IBAN : 4 derniers caractères).
- Données de test fictives ; ne jamais ouvrir `extraits/` ni un `*.xlsx`.
- Commits en français terminés par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Venv : `C:/Users/<utilisateur>/Documents/Perso/Applications/Extranalyzer/.venv`.

## Review Focus

- Resynchro sur une fenêtre qui recouvre la précédente : aucune ligne en double.
- Retour de la banque avec `state` inconnu, expiré ou rejoué : refusé, aucun lien créé.
- Échec réseau sur un compte au milieu d'une synchro : les autres comptes écrivent, celui-ci rien.
- Fonctionnalité non configurée : l'appli démarre et l'onglet l'explique.
- PDF importé après coup sur une période déjà couverte par l'API : section en erreur.

---

### Task 1 : schéma v6 et colonne `source`

**Files :** Modify `app/store/schema.sql`, `app/store/db.py`, `app/store/repo.py` ; Test `tests/test_store.py`

**Produces :** tables `bank_links`, `bank_auth_states`, `external_accounts`, `external_transactions`, `sync_runs`
(spec § 5.12) ; `transactions.source TEXT NOT NULL DEFAULT 'pdf'` ; `SCHEMA_VERSION = 6` ;
`repo.insert_transaction(..., source: str = "pdf")` avec `section_id: int | None`.

- [ ] Tests : base neuve en v6 avec les tables ; migration d'une base v5 qui garde les lignes avec `source='pdf'`.
- [ ] Implémenter, suite verte, commit.

### Task 2 : couche générique `app/sources/`

**Files :** Create `app/sources/__init__.py`, `app/sources/base.py`, `app/sources/service.py` ; Modify
`app/importer/service.py` (garde-fou) ; Test `tests/test_sources_service.py`, `tests/test_import_service.py`

**Produces :**
- `FetchedTransaction(op_date: date, value_date: date | None, label: str, amount: Decimal, raw: dict)`
- `FetchedBalance(amount: Decimal, reference_date: date)`
- `AccountFetch(account_id: int, transactions: list[FetchedTransaction], balance: FetchedBalance | None, rejected: int = 0, error: str | None = None)`
- `frontier(conn, account_id) -> date | None`
- `store_fetch(conn, source: str, fetch: AccountFetch) -> tuple[list[int], int]` (ids insérés, ignorés avant frontière)
- `balance_gap(conn, account_id, balance) -> int | None` (centimes : attendu − banque − écart accepté… voir spec § 5.8)
- `repo.api_overlap(conn, account_id, statement_date) -> bool`

- [ ] Tests : frontière stricte ; rang des doublons exacts indépendant de l'ordre ; resynchro sans doublon ;
  reclassement manuel ré-appliqué ; JSON brut stocké ; écart de solde (égal, écart, sans PDF → None) ; PDF dont la
  période contient une opération API → section `error` ; PDF antérieur à la première opération API → `ok`.
- [ ] Implémenter, suite verte, commit.

### Task 3 : JWT, client et conversion Enable Banking

**Files :** Create `app/sources/enablebanking/{__init__,jwt,client,convert}.py` ; Test `tests/test_enablebanking_client.py`

**Produces :**
- `make_jwt(app_id: str, key_pem: bytes, now: int, lifetime: int = 3600) -> str`
- `Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]`, `urllib_transport`
- `EnableBankingError(status, code, message)`, sous-classes `SessionExpired` (401), `RateLimited` (429)
- `Client(app_id, key_pem, transport, clock=time.time)` : `aspsps(country, psu_type)`, `start_auth(...)`,
  `create_session(code)`, `get_session(id)`, `delete_session(id)`, `balances(uid)`, `transactions(uid, date_from, strategy)` (pagine)
- `convert_transaction(tx: dict) -> FetchedTransaction | None`, `pick_balance(balances: list[dict]) -> FetchedBalance | None`

- [ ] Tests : JWT vérifié avec une clé générée ; pagination ; 401/429/500 typées ; BOOK seulement, signe DBIT,
  devise non EUR et sans date rejetées, libellé de repli ; CLBD puis ITBD, sinon None.
- [ ] Implémenter, suite verte, commit.

### Task 4 : connexion, rattachement, synchro

**Files :** Create `app/sources/enablebanking/link.py` ; Modify `app/config.py` ; Test `tests/test_enablebanking_link.py`

**Produces :**
- `BankSettings(app_id, key_pem, redirect_url, aspsp_name="Crédit Mutuel", country="FR")`, `Settings.bank: BankSettings | None`, `Settings.sync_hours: int`
- `start_connection(conn, client, bank, now) -> str` (URL de la banque)
- `complete_connection(conn, client, code, state, now) -> int` (`StateError` si `state` invalide)
- `attach(conn, external_id, account_id | None, *, ignore=False)`
- `fetch_all(conn, client, today) -> list[AccountFetch]` (lecture seule de la base, réseau)
- `write_sync(conn, fetches, trigger, on_new, now) -> int` (id de `sync_runs`)
- `bank_status(conn, now) -> dict | None` (alerte pour `/api/meta`)

- [ ] Tests : `state` inconnu/expiré/rejoué ; rattachement auto par IBAN, aucun candidat → compte créé
  (`savings` si SVGS), plusieurs → à rattacher ; renouvellement qui retrouve par `identification_hash` ; fenêtre
  `date_from` ; 401 → lien `expired` ; 429 → `retry_after` +6 h ; échec d'un compte isolé ; une passe de classement ;
  appariement courant ↔ livret ; statut 14 jours / expiré / erreur.
- [ ] Implémenter, suite verte, commit.

### Task 5 : routes, planificateur, alerte, onglet Banque

**Files :** Create `app/tabs/banque.py`, `app/sources/scheduler.py`, `app/web/tabs/banque.js`,
`app/web/bank-callback.html`, `app/web/bank-callback.js` ; Modify `app/main.py`, `app/tabs/__init__.py`,
`app/web/app.js`, `app/web/index.html`, `app/web/style.css` ; Test `tests/test_banque_api.py`

- [ ] Tests : sans configuration → statut `configured: false` ; connexion → URL ; retour (faux client) → lien
  créé et synchro lancée ; synchro manuelle ; rattacher, ignorer, accepter un écart ; déconnexion ; `/api/meta.bank` ;
  `/bank/callback` servi sans session ; un tour du planificateur appelé directement.
- [ ] Implémenter, suite verte, commit.

### Task 6 : documentation et déploiement

**Files :** Modify `README.md`, `docker-compose.yml`

- [ ] Secret `enablebanking_key`, variables `EXTRANALYZER_EB_*`, `EXTRANALYZER_SYNC_HOURS`, marche à suivre pour
  créer l'application et lier ses comptes ; spec passée « validée ».
- [ ] Suite complète verte, commit.
