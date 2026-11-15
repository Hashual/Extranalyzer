# Tickets de caisse et détail des courses : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** relever les tickets de caisse dans Gmail (et par dépôt manuel), les lire (Super U), les
relier aux débits carte et montrer la consommation par famille, rayon et produit dans un onglet Courses.

**Architecture :** un paquet `app/receipts/` isolé du cœur :
- modèle ;
- lecteurs par enseigne ;
- service d'ingestion ;
- rapprochement ;
- rayons ;
- relève IMAP en thread de fond.

Le cœur ne fait que trois choses : il appelle le rapprochement après chaque import ou ré-analyse, il
reporte les liens quand un `tx_key` change, et il démarre la relève. L'onglet `courses` suit le
contrat d'onglet existant.

**Tech Stack :** Python 3.12, FastAPI, SQLite, `pdfplumber`, `imaplib`/`email` (stdlib), JS vanilla
en modules ES ; tests avec `pytest`, `reportlab` (PDF fictifs), `TestClient`.

**Spec :** `docs/superpowers/specs/2026-11-15-tickets-de-caisse-design.md`

## Global Constraints

- Aucune nouvelle dépendance : `imaplib`, `email`, `tomllib`, `pdfplumber` seulement.
- Données réelles jamais commitées : PDF, adresses, magasins, montants. Les tests utilisent des
  tickets **fictifs** générés.
- Les données réelles de développement vivent dans `data/` (ignoré par git). Le mot de passe
  d'application est lu dans `../../../secrets/gmail_app_password.txt` (dossier principal du dépôt,
  ignoré par git), **les espaces retirés**.
- Gmail est ouvert **en lecture seule** : `select(..., readonly=True)` et `BODY.PEEK[]`. Aucun
  drapeau n'est modifié.
- Hôte IMAP `imap.gmail.com`, port 993, TLS avec certificat vérifié
  (`ssl.create_default_context()`).
- Libellés par défaut : `Super U/Tickets de caisses=superu;intermarche/Tickets de caisses=intermarche`.
  Intervalle par défaut : 60 min.
- Fenêtre de rapprochement : date du ticket → +3 jours, montant exact, débits des comptes `checking`,
  hors jambes miroir. Liste manuelle des candidats : ±7 jours.
- Le rapprochement ne change jamais la catégorie d'un débit. Les totaux des autres onglets ne
  changent pas.
- Mémoire : les PDF sont lus un par un.
- Toute écriture de fond passe par le `write_lock` de `create_app`.
- Journaux : jamais d'adresse mail, de mot de passe, de libellé produit ni de montant.

## Review Focus

1. **Gmail injoignable ou mot de passe refusé au démarrage** : l'app démarre quand même, l'état de
   relève affiche l'erreur, et le prochain passage réessaie (Task 2 :
   `test_sync_auth_error_is_recorded_not_raised`).
2. **La même relève lancée deux fois, ou un message déjà vu** : aucun doublon, `last_uid` avance
   (Task 2 : `test_sync_is_incremental_and_idempotent`).
3. **Deux tickets de même montant le même jour** (deux passages dans la journée) : aucun lien
   automatique au hasard, les deux attendent une décision (Task 4 :
   `test_two_same_amount_receipts_same_day_wait`).
4. **Article pesé ou remise sur la ligne suivante** : le ticket reste `ok`, et la somme des lignes
   égale le total (Task 3 : `test_weighed_item_and_discount_lines`).
5. **Débit relié qui disparaît à la ré-analyse** : un lien auto est défait puis refait si possible ;
   un lien manuel est marqué cassé (Task 4 : `test_vanished_debit_auto_vs_manual`).

---

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| `app/store/schema.sql`, `app/store/db.py` | schéma v5 (nouvelles tables) et migration 4 → 5 |
| `app/config.py` | réglages Gmail, libellés, intervalle |
| `app/receipts/model.py` | `ParsedReceipt`, `ReceiptLine`, `Payment`, `ReceiptParseError` |
| `app/receipts/base.py` | protocole `ReceiptParser` |
| `app/receipts/superu.py` | lecteur Super U (coordonnées pdfplumber) |
| `app/receipts/service.py` | ingestion (dédoublonnage, stockage, appariement ticket CB, lecture), relecture |
| `app/receipts/aisles.py`, `app/receipts/aisles.seed.toml` | familles, rayons, seed |
| `app/receipts/matching.py` | rapprochement auto, lien manuel, candidats |
| `app/receipts/mailsync.py` | relève IMAP, état par libellé, thread de fond |
| `app/store/repo.py` | `move_receipt_links` |
| `app/importer/service.py` | appel de `move_receipt_links` dans `_carry_overrides` |
| `app/main.py` | répertoire `receipts/`, seed rayons, rappel après import, thread de relève, garde de taille |
| `app/tabs/courses.py`, `app/tabs/__init__.py` | API de l'onglet |
| `app/tabs/_transactions.py`, `app/web/ui.js` | `receipt_id` et icône 🧾 |
| `app/web/tabs/courses.js`, `app/web/style.css` | interface de l'onglet |
| `scripts/fetch_receipts.py`, `scripts/check_real_receipts.py` | relève locale et vérification sur vrais tickets (compteurs seulement) |
| `tests/receiptgen.py` + `tests/test_receipts_*.py`, `tests/test_courses_api.py` | tests |
| `docker-compose.yml`, `README.md` | secret et documentation |

---

### Task 1 : schéma v5 et configuration Gmail

**Files:**
- Modify: `app/store/schema.sql`, `app/store/db.py`, `app/config.py`
- Test: `tests/test_store.py`, `tests/test_config.py` (créer si absent ; sinon ajouter)

**Interfaces:**
- Produces, tables (dans `schema.sql`, `PRAGMA user_version = 5`, et dans `_MIGRATE_4_TO_5`) :
  - `receipts(id INTEGER PK, store TEXT NOT NULL, stamp TEXT NULL, sha256 TEXT UNIQUE NOT NULL, stored_path TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('imap','upload')), message_id TEXT NULL UNIQUE, shop TEXT NULL, purchased_at TEXT NULL, total_cents INTEGER NULL, status TEXT NOT NULL CHECK(status IN ('ok','error','pending_parser')), message TEXT NULL, tx_key TEXT NULL, link_mode TEXT NULL CHECK(link_mode IN ('auto','manual')), link_broken INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL)`, plus l'index `idx_receipts_tx_key` ;
  - `receipt_slips(id INTEGER PK, sha256 TEXT UNIQUE NOT NULL, stored_path TEXT NOT NULL, stamp TEXT NULL, receipt_id INTEGER NULL REFERENCES receipts(id))` (tickets CB) ;
  - `receipt_lines(receipt_id INTEGER NOT NULL REFERENCES receipts(id), rank INTEGER NOT NULL, aisle_id INTEGER NULL REFERENCES aisles(id), label TEXT NOT NULL, quantity REAL NOT NULL, unit_cents INTEGER NULL, amount_cents INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('item','discount')), PRIMARY KEY(receipt_id, rank))` ;
  - `receipt_payments(receipt_id INTEGER NOT NULL REFERENCES receipts(id), rank INTEGER NOT NULL, method TEXT NOT NULL CHECK(method IN ('card','meal_voucher','voucher','cash','other')), amount_cents INTEGER NOT NULL, PRIMARY KEY(receipt_id, rank))` ;
  - `aisle_families(id INTEGER PK, name TEXT UNIQUE NOT NULL, position INTEGER NOT NULL)` ;
  - `aisles(id INTEGER PK, store TEXT NOT NULL, name TEXT NOT NULL, family_id INTEGER NULL REFERENCES aisle_families(id), UNIQUE(store, name))` ;
  - `mail_sync(label TEXT PRIMARY KEY, store TEXT NOT NULL, uidvalidity INTEGER NULL, last_uid INTEGER NOT NULL DEFAULT 0, last_success_at TEXT NULL, last_attempt_at TEXT NULL, last_error TEXT NULL)`.
- `SCHEMA_VERSION = 5`.
- Produces, dans `app/config.py` :
  - `@dataclass(frozen=True) class MailSettings: user: str; app_password: str; labels: tuple[tuple[str, str], ...]; interval_min: int = 60` ;
  - `Settings.mail: MailSettings | None = None` ;
  - `def mail_settings() -> MailSettings | None`, qui lit :
    - `EXTRANALYZER_GMAIL_USER` ;
    - `EXTRANALYZER_GMAIL_APP_PASSWORD` ou `EXTRANALYZER_GMAIL_APP_PASSWORD_FILE`, en retirant tous
      les espaces ; les deux à la fois → `ConfigError` ;
    - `EXTRANALYZER_RECEIPT_LABELS`, au format `libellé=enseigne;…` ;
    - `EXTRANALYZER_MAIL_INTERVAL_MIN`, entier > 0.
  - Utilisateur vide **ou** mot de passe vide (fichier vide compris) → `None`, relève désactivée.
- `load_settings()` remplit `mail=mail_settings()`.

- [ ] **Step 1 : tests en échec**

```python
def test_fresh_db_is_v5_with_receipt_tables(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    for t in ("receipts", "receipt_slips", "receipt_lines", "receipt_payments", "aisles", "aisle_families", "mail_sync"):
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (t,)).fetchone()

def test_v4_db_migrates_to_v5(tmp_path): ...  # base v4 créée avec le schéma v4 (copie figée dans le test), connect() -> v5, tables présentes, données conservées

def test_mail_settings_strip_spaces_and_defaults(monkeypatch, tmp_path):
    f = tmp_path / "pw"; f.write_text("abcd efgh ijkl mnop\n")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_USER", "test@example.invalid")
    monkeypatch.setenv("EXTRANALYZER_GMAIL_APP_PASSWORD_FILE", str(f))
    m = mail_settings()
    assert m.app_password == "abcdefghijklmnop"
    assert m.labels == (("Super U/Tickets de caisses", "superu"), ("intermarche/Tickets de caisses", "intermarche"))
    assert m.interval_min == 60

def test_mail_settings_disabled_when_empty(monkeypatch, tmp_path):  # pas d'utilisateur -> None ; fichier vide -> None

def test_mail_settings_both_password_sources_is_error(monkeypatch, tmp_path): ...  # ConfigError
```

- [ ] **Step 2 : lancer et voir échouer** : `.venv/Scripts/python -m pytest tests/test_store.py tests/test_config.py -q`. Attendu : FAIL.
- [ ] **Step 3 : implémenter** schéma, migration et `mail_settings` comme décrit ci-dessus.
- [ ] **Step 4 : toute la suite passe** : `.venv/Scripts/python -m pytest -q`. Attendu : tout PASS.
- [ ] **Step 5 : commit** `feat: schéma v5 des tickets de caisse et réglages Gmail`

---

### Task 2 : ingestion et relève IMAP (sans lecteur)

**Files:**
- Create: `app/receipts/__init__.py`, `app/receipts/model.py`, `app/receipts/base.py`,
  `app/receipts/service.py`, `app/receipts/mailsync.py`, `scripts/fetch_receipts.py`
- Test: `tests/test_receipts_service.py`, `tests/test_mailsync.py`

**Interfaces:**
- Produces, dans `model.py` :
  - `@dataclass class ReceiptLine: aisle: str; label: str; quantity: float; unit_cents: int | None; amount_cents: int; kind: str  # 'item' | 'discount'` ;
  - `@dataclass class Payment: method: str; amount_cents: int` ;
  - `@dataclass class ParsedReceipt: shop: str; purchased_at: datetime; total_cents: int; lines: list[ReceiptLine]; payments: list[Payment]` ;
  - `class ReceiptParseError(ValueError)`.
- Produces, dans `base.py` : `class ReceiptParser(Protocol): store: str; def kind(self, pdf: Path) -> str | None  # 'receipt' | 'cb_slip' | None` et `def parse(self, pdf: Path) -> ParsedReceipt`.
- Produces, dans `service.py` :
  - `@dataclass class FileResult: name: str; outcome: str; receipt_id: int | None = None; status: str | None = None; message: str | None = None` ;
    `outcome` ∈ `stored`, `slip`, `duplicate`, `rejected`.
  - `def ingest_file(conn, data: bytes, filename: str, receipts_dir: Path, parsers: list[ReceiptParser], *, origin: str, store: str | None = None, message_id: str | None = None) -> FileResult`. Comportement :
    - dédoublonnage par SHA-256 dans `receipts` et `receipt_slips` ;
    - stockage dans `receipts_dir/<sha256>.pdf` ;
    - `stamp` = regex `(\d{8}-\d{6})` sur le nom de fichier ;
    - nature : `kind()` du premier lecteur qui répond, sinon le nom commence par `Ticket de CB` →
      ticket CB ;
    - un ticket CB est rattaché au ticket de même `stamp` s'il existe ; sinon il attend, et un ticket
      arrivé plus tard reprend les tickets CB en attente de même `stamp` ;
    - `store` connu sans lecteur → `pending_parser` ;
    - aucun lecteur et pas de `store` (dépôt manuel) → `rejected`, avec le message « enseigne pas
      encore prise en charge », et rien n'est stocké ;
    - échec de lecture → `error` + message.
  - `def write_parsed(conn, receipt_id: int, parsed: ParsedReceipt) -> None` : contrôles bloquants
    `Σ lines == total_cents` et `Σ payments == total_cents`, sinon `ReceiptParseError("écart de X,XX €")`.
    Remplace lignes et paiements. Rayons via `aisles.aisle_id` (Task 3). Dans cette tâche, `aisle_id`
    reste NULL.
  - `def reread(conn, receipts_dir: Path, parsers, only_pending: bool = False) -> Counter` : relit les
    tickets (tous, ou seulement `pending_parser` dont l'enseigne a désormais un lecteur).
- Produces, dans `mailsync.py` :
  - `@dataclass class FetchedMessage: label: str; store: str; uidvalidity: int; uid: int; message_id: str | None; attachments: list[tuple[str, bytes]]` (PDF seulement) ;
  - `def fetch_new(cfg: MailSettings, state: dict[str, tuple[int | None, int]], connect=imaplib.IMAP4_SSL) -> Iterator[FetchedMessage]`. IMAP :
    - `login` ;
    - pour chaque libellé : `select('"<libellé>"', readonly=True)`, puis lecture de l'`UIDVALIDITY` ;
      s'il diffère de l'état, on repart de l'UID 0 ;
    - `uid('SEARCH', f'UID {last+1}:*')`, en ignorant les UID ≤ last ;
    - `uid('FETCH', uid, '(BODY.PEEK[])')` ;
    - `email.message_from_bytes(..., policy=email.policy.default)`.
  - `def sync_once(cfg: MailSettings, write: Callable, receipts_dir: Path, parsers, connect=imaplib.IMAP4_SSL) -> dict` :
    - lit l'état `mail_sync` ;
    - chaque message est ingéré dans **sa propre** transaction `write(...)` :
      - `ingest_file(origin='imap', store=..., message_id=...)` pour chaque pièce jointe ;
      - un `message_id` déjà présent → ignoré ;
      - mise à jour de `last_uid` et `uidvalidity` dans la même transaction.
    - Fin du passage → `last_success_at`. Toute exception IMAP, OSError ou `ssl.SSLError` →
      `last_error` = type d'erreur et message court sans données, `last_attempt_at` mis à jour, et
      **pas** de propagation. Renvoie `{"messages": n, "files": n, "error": str | None}`.
  - `class MailSyncThread(threading.Thread)` (daemon) : `run()` appelle `sync_once` au démarrage, puis
    toutes les `interval_min`. `trigger()` déclenche un passage immédiat. `stop()` sert aux tests.
    Les passages sont sérialisés par un verrou interne.

- [ ] **Step 1 : tests en échec** (faux client IMAP en mémoire : `FakeImap` avec `login`, `select`,
  `response('UIDVALIDITY')` ou une réponse de `select`, `uid('SEARCH'|'FETCH')`, et qui enregistre
  les commandes reçues)

```python
def test_sync_is_incremental_and_idempotent(tmp_path):
    # 2 messages dans "Super U/Tickets de caisses", chacun avec un ticket et un ticket CB
    # (PDF fictifs minimaux) ; sync_once deux fois de suite.
    # receipts : 2 lignes, status 'pending_parser' (aucun lecteur), store 'superu' ;
    # 2 receipt_slips rattachés ; mail_sync.last_uid == 2.
    # Au 2e passage, la recherche porte sur 'UID 3:*' et rien n'est ajouté.

def test_sync_never_writes_flags(tmp_path):
    # Les commandes reçues contiennent select(readonly=True) et 'BODY.PEEK[]',
    # jamais STORE, COPY, EXPUNGE ni 'BODY[]' sans PEEK.

def test_uidvalidity_change_restarts_from_zero(tmp_path): ...   # aucun doublon grâce à message_id et sha256

def test_sync_auth_error_is_recorded_not_raised(tmp_path):
    # Le faux login lève imaplib.IMAP4.error.
    # sync_once renvoie error non vide ; mail_sync.last_error est rempli ;
    # last_success_at reste NULL ; aucune exception.

def test_upload_without_parser_or_store_is_rejected(tmp_path):
    r = ingest_file(conn, pdf_bytes, "x.pdf", tmp_path, [], origin="upload")
    assert r.outcome == "rejected" and "pas encore prise en charge" in r.message
    assert not list(tmp_path.glob("*.pdf"))

def test_slip_waits_then_attaches_to_later_receipt(tmp_path): ...   # 'Ticket de CB_01012025-101010.pdf' puis le ticket de même stamp
def test_duplicate_file_is_skipped(tmp_path): ...
```

- [ ] **Step 2 : lancer** `.venv/Scripts/python -m pytest tests/test_receipts_service.py tests/test_mailsync.py -q`. Attendu : FAIL.
- [ ] **Step 3 : implémenter** `model.py`, `base.py`, `service.py` (sans rayons) et `mailsync.py`.
- [ ] **Step 4 : `scripts/fetch_receipts.py <data_dir>`**. Le script :
  - lit `EXTRANALYZER_GMAIL_USER` et le fichier de mot de passe (`mail_settings()`) ;
  - ouvre `<data_dir>/extranalyzer.db` et `<data_dir>/receipts/` ;
  - lance `sync_once` avec un `write` local (connexion, commit, fermeture) ;
  - n'affiche que les compteurs.
- [ ] **Step 5 : tests PASS**, puis suite complète `.venv/Scripts/python -m pytest -q`.
- [ ] **Step 6 : relève réelle en local** (données hors git) :
  `EXTRANALYZER_GMAIL_USER=<compte des tickets> EXTRANALYZER_GMAIL_APP_PASSWORD_FILE=../../../secrets/gmail_app_password.txt .venv/Scripts/python scripts/fetch_receipts.py data/dev`.
  Attendu : environ 99 messages et environ 198 fichiers, `error: None`. Un second passage donne
  0 message. Si l'authentification échoue, s'arrêter et demander à l'utilisateur.
- [ ] **Step 7 : commit** `feat: relève IMAP des tickets de caisse et ingestion`

---

### Task 3 : lecteur Super U, rayons et familles

**Files:**
- Create: `app/receipts/superu.py`, `app/receipts/aisles.py`, `app/receipts/aisles.seed.toml`,
  `tests/receiptgen.py`, `scripts/check_real_receipts.py`
- Modify: `app/receipts/service.py` (rayons dans `write_parsed`)
- Test: `tests/test_receipts_superu.py`, `tests/test_receipts_aisles.py`

**Interfaces:**
- Produces :
  - `class SuperUParser` : `store = "superu"`, `kind()`, `parse()` ;
  - `PARSERS_RECEIPTS: list[ReceiptParser] = [SuperUParser()]` dans `app/receipts/__init__.py`.
- Produces, dans `aisles.py` :
  - `def normalize_aisle(name: str) -> str` : `repo.fold`, espaces fusionnés, bords retirés ;
  - `def load_aisle_seed(conn, path: Path) -> None` : familles idempotentes, rayons insérés s'ils
    manquent, la famille d'un rayon existant n'est jamais écrasée ;
  - `def aisle_id(conn, store: str, name: str) -> int` : crée le rayon sans famille s'il est inconnu.
- `aisles.seed.toml` :
  - `[[family]] name=…` dans l'ordre de la spec § 5.5 : Crèmerie, Fruits & légumes, Viandes &
    poissons, Épicerie salée, Épicerie sucrée, Boissons, Surgelés, Boulangerie & pâtisserie,
    Hygiène & beauté, Entretien, Maison & divers, Animaux, Remises ;
  - `[aisles.superu]` : `"NOM RAYON" = "Famille"`.

- [ ] **Step 1 : reconnaissance sur les vrais tickets** (rien de réel n'est commité ni gardé en
  mémoire persistante). Écrire un script jetable dans le scratchpad qui affiche, pour 5 à 10 tickets
  variés de `data/dev/receipts/`, les mots pdfplumber (`x0`, `x1`, `top`, `fontname`) et les
  rectangles et lignes horizontales. Noter **la structure seulement** :
  - comment on reconnaît un titre de rayon : police grasse et/ou trait sous le titre ;
  - les colonnes des montants ;
  - le format des articles pesés (`kg x €/kg`) ;
  - les lignes de remise, de fidélité et de bon ;
  - le bloc des totaux ;
  - les libellés de paiement : CB, titre-restaurant ou carte TR, cagnotte, espèces avec rendu ;
  - la date et l'heure ; le magasin ;
  - ce qui distingue un ticket CB d'une carte bancaire de celui d'une carte titre-restaurant.

  Lister aussi tous les titres de rayon distincts des 99 tickets (`set`). Ces noms sont génériques,
  ils serviront au seed.
- [ ] **Step 2 : `tests/receiptgen.py`**, `make_superu_receipt(path, *, shop="MAGASIN TEST", when=datetime(...), aisles: list[tuple[str, list[Item]]], discounts=…, payments=[("CB", 1234), ("TITRE RESTAURANT", 500)])`.
  Il reproduit la géométrie, les polices et les traits relevés au Step 1, avec des libellés fictifs.
  Il fournit aussi `make_superu_slip(path, kind="card"|"meal_voucher", amount_cents=…)`.
- [ ] **Step 3 : tests en échec**

```python
def test_parse_basic_receipt(tmp_path):   # 2 rayons, 3 articles, dont une quantité 2 x 1,50
    p = SuperUParser().parse(make_superu_receipt(tmp_path / "t.pdf", ...))
    assert p.shop == "MAGASIN TEST" and p.purchased_at == datetime(2025, 1, 8, 15, 10, 1)
    assert [(l.aisle, l.label, l.quantity, l.unit_cents, l.amount_cents) for l in p.lines] == [...]
    assert p.total_cents == sum(l.amount_cents for l in p.lines)

def test_weighed_item_and_discount_lines(tmp_path): ...   # quantité 0.532, remise kind='discount' négative, ticket ok
def test_payments_split_card_and_meal_voucher(tmp_path):
    assert [(x.method, x.amount_cents) for x in p.payments] == [("card", 1234), ("meal_voucher", 500)]
def test_cash_with_change_is_net(tmp_path): ...           # espèces 20,00, rendu 3,50 -> cash 1650
def test_kind_detects_receipt_and_slip(tmp_path): ...     # 'receipt' / 'cb_slip' / None pour un relevé bancaire fictif (tests/pdfgen)
def test_total_mismatch_marks_receipt_error(tmp_path):    # via ingest_file : status 'error', message 'écart de', 0 ligne

def test_unknown_aisle_is_created_without_family(tmp_path): ...
def test_seed_never_overwrites_user_family(tmp_path): ...
def test_normalize_aisle(): assert normalize_aisle("  Fromage   LS ") == "FROMAGE LS"
```

- [ ] **Step 4 : lancer** `.venv/Scripts/python -m pytest tests/test_receipts_superu.py tests/test_receipts_aisles.py -q`. Attendu : FAIL.
- [ ] **Step 5 : implémenter** `SuperUParser`, avec les règles du Step 1 dans la docstring (structure
  seulement), ainsi que `aisles.py`. Dans `write_parsed`, chaque ligne reçoit
  `aisle_id(conn, store, line.aisle)`.
  - Une remise sans rayon visible est rattachée au rayon `REMISES`, rangé dans la famille « Remises »
    par le seed.
  - Méthodes de paiement : CB → `card`, titres-restaurant (carte TR, Swile, titre restaurant,
    etc., selon le Step 1) → `meal_voucher`, bons et cagnotte → `voucher`, espèces nettes du rendu →
    `cash`, autre → `other`.
  - Si le ticket ne distingue pas les cartes, le ticket CB rattaché sert à reclasser un paiement
    `card` en `meal_voucher`.
- [ ] **Step 6 : seed** : renseigner `[aisles.superu]` avec **tous** les rayons listés au Step 1,
  chacun rangé dans une famille.
- [ ] **Step 7 : `scripts/check_real_receipts.py <data_dir>`**. Il relit tous les tickets dans une
  base jetable et n'affiche que des compteurs : `ok`, `error` (par type), lignes, rayons sans
  famille, tickets avec titre-restaurant. Le lancer sur `data/dev`. Attendu : 99 `ok`, 0 rayon sans
  famille. Sinon, corriger le lecteur et ajouter au générateur le cas manquant, avec un test.
- [ ] **Step 8 : suite complète PASS**, puis commit `feat: lecteur des tickets Super U, rayons et familles`

---

### Task 4 : rapprochement ticket ↔ débit

**Files:**
- Create: `app/receipts/matching.py`
- Modify: `app/store/repo.py`, `app/importer/service.py` (`_carry_overrides`), `app/main.py`
  (rappel après import, conflit et ré-analyse)
- Test: `tests/test_receipts_matching.py`, `tests/test_import_service.py` (report des liens)

**Interfaces:**
- Produces, dans `matching.py` :
  - `def card_cents(conn, receipt_id: int) -> int` : Σ `receipt_payments` de méthode `card` ;
  - `def match_receipts(conn) -> dict` (ne valide pas), dans l'ordre :
    1. **Liens dont le `tx_key` n'existe plus** : un lien auto est retiré (`tx_key`, `link_mode` à
       NULL) ; un lien manuel passe à `link_broken=1`.
    2. **Liens auto dont le montant ne correspond plus** à `-card_cents` : retirés.
    3. **Tickets `ok`** sans lien, `link_broken=0` et `card_cents > 0`, triés par `purchased_at`, `id` :
       - candidats = transactions `t JOIN accounts a` avec `a.kind='checking'`,
         `t.mirror_of IS NULL`, `t.amount_cents = -card_cents`,
         `t.op_date BETWEEN date(purchased_at) AND date(purchased_at, '+3 days')`, et `tx_key` non
         relié par un autre ticket (y compris un lien manuel cassé) ;
       - un seul candidat → lien `auto` ;
       - plusieurs → on garde ceux dont `label_norm` (fold) contient le jeton `U` isolé ou un mot de
         `shop` de 3 lettres ou plus ; s'il en reste exactement un → lien `auto`, sinon rien.

    Renvoie `{"linked": n, "unlinked": n, "broken": n}`.
  - `def link_manual(conn, receipt_id: int, tx_key: str | None) -> None` : `None` délie ; un débit
    déjà relié ailleurs → `ValueError` ; met `link_mode='manual'` et `link_broken=0`.
  - `def candidates(conn, receipt_id: int, limit: int = 30) -> list[dict]` : d'abord même montant à
    ±7 jours, puis débits `checking` à ±7 jours, triés par écart de date. Chaque entrée :
    `{tx_key, op_date, label, amount}`.
- Produces, dans `repo.py` : `def move_receipt_links(conn, moves: dict[str, str]) -> None`.
  Elle déplace `receipts.tx_key` de l'ancienne clé vers la nouvelle, sauf si la nouvelle est déjà
  reliée.
- Dans `_carry_overrides`, appeler `repo.move_receipt_links(conn, moves)` avec les mêmes `moves`.
- Dans `main.py` :

  ```python
  def after_import(conn, ids):
      on_new_transactions(conn, ids)
      match_receipts(conn)
  ```

  Elle remplace `on_new_transactions` dans `import_pdf`, `resolve_conflict` et `reanalyse_all`.

- [ ] **Step 1 : tests en échec** (base créée par `connect`, comptes et transactions insérés
  directement, tickets insérés avec `ingest_file` sur des PDF fictifs, ou directement en SQL)

```python
def test_single_candidate_links_auto(conn): ...            # débit -28,28 le lendemain -> link_mode 'auto'
def test_two_same_amount_receipts_same_day_wait(conn): ... # deux tickets 12,00 et deux débits 12,00 le même jour -> aucun lien
def test_label_tiebreak_picks_store_debit(conn): ...       # 'CB HYPER U VILLETEST' contre 'CB PHARMACIE' -> relié au premier
def test_meal_voucher_only_receipt_has_no_debit(conn): ... # card_cents 0 -> jamais relié, status ok
def test_partial_meal_voucher_matches_card_part(conn): ... # total 30,00 = CB 18,00 + TR 12,00 -> relié au débit -18,00
def test_manual_link_is_kept_and_blocks_auto(conn): ...
def test_vanished_debit_auto_vs_manual(conn):
    # On supprime la transaction reliée.
    # Le lien auto est retiré, puis refait si un autre candidat unique existe.
    # Le lien manuel garde son tx_key, avec link_broken == 1, et n'est jamais refait automatiquement.

def test_reanalysis_moves_receipt_links(tmp_path): ...     # dans test_import_service : un tx_key qui change emporte receipts.tx_key
def test_link_manual_rejects_already_linked_debit(conn): ...
```

- [ ] **Step 2 : lancer et voir échouer** :
  `.venv/Scripts/python -m pytest tests/test_receipts_matching.py tests/test_import_service.py -q`.
- [ ] **Step 3 : implémenter** `matching.py`, `move_receipt_links`, l'appel dans
  `_carry_overrides` et `after_import`.
- [ ] **Step 4 : suite complète PASS**, puis commit `feat: rapprochement des tickets avec les débits carte`

---

### Task 5 : API de l'onglet Courses et câblage de l'application

**Files:**
- Create: `app/tabs/courses.py`
- Modify:
  - `app/tabs/__init__.py` : `"courses"` après `"categories"` ;
  - `app/main.py` : `receipts/` dans `_init_storage`, `load_aisle_seed`,
    `reread(only_pending=True)` puis `match_receipts` au démarrage, thread de relève, garde de
    taille ;
  - `app/tabs/_transactions.py` : `receipt_id`.
- Test: `tests/test_courses_api.py`, `tests/test_tabs_api.py` (ordre des onglets, `receipt_id`)

**Interfaces:**
- Consumes : `ingest_file`, `reread`, `PARSERS_RECEIPTS`, `match_receipts`, `link_manual`,
  `candidates`, `sync_once`, `MailSyncThread`, `Scope`.
- `create_app(settings, enabled_tabs=None, start_mail_thread: bool = True)` :
  - le thread n'est démarré que si `settings.mail` n'est pas `None` et que `start_mail_thread` est
    vrai ;
  - `app.state` reçoit `receipts_dir`, `write` (la fonction verrouillée existante), `mail_thread`
    (ou None) et `mail_enabled`.
- Garde de taille : la règle de `/api/upload` s'applique aussi à `/api/tabs/courses/upload`, avec
  une limite de `max_upload_mb` pour la requête entière.
- `TAB = Tab("courses", "Courses", 25, router)`. Routes sous `/api/tabs/courses/` :
  - `GET /summary` (Scope) → `{total, by_payment: {card, meal_voucher, voucher, cash, other}, families: [{id, name, amount, compare}], undetailed, undetailed_compare, counts: {to_link, broken, unsorted_aisles, errors, pending_parser}, out_of_courses: [{receipt_id, tx_label, category_name}]}`. Définitions :
    - **tickets comptés** : `status='ok'` et `date(purchased_at)` dans la période ; ils ne dépendent
      pas du sélecteur de comptes ;
    - **familles** : Σ `amount_cents` de leurs lignes ; une famille NULL est affichée « À ranger » ;
    - **undetailed** : débits nets de la catégorie racine « Courses » et de ses sous-catégories, sur
      le périmètre `scope_clause`, dont le `tx_key` n'est relié à aucun ticket (`link_broken=0`) ;
      montant positif = dépense ;
    - **total** : Σ des familles + `undetailed` ;
    - **by_payment** : Σ des paiements des tickets comptés, avec `undetailed` ajouté à `card` ;
    - **compare** : mêmes calculs sur `s.compare` (null sans comparaison) ;
    - **out_of_courses** : tickets comptés dont le débit relié n'est pas dans le sous-arbre
      « Courses ».
  - `GET /family/{family_id}` (Scope ; `0` = « À ranger ») →
    `{aisles: [{id, name, store, amount, compare, products: [{label, quantity, amount, avg_unit, purchases}]}]}`,
    où `purchases` est le nombre de tickets distincts et `avg_unit` = amount / quantity, arrondi au
    centime.
  - `GET /receipts` (Scope) → tickets de la période **plus** tous ceux à relier ou cassés, quelle que
    soit leur date. Chaque entrée : `{id, store, shop, purchased_at, total, card, status, link: 'auto'|'manual'|'to_link'|'broken'|'no_card'|null, message}`.
  - `GET /receipts/{id}` → `{…mêmes champs, lines par rayon: [{aisle, family, lines: [...]}], payments, tx: {tx_key, op_date, label, amount} | null, candidates}`, 404 si inconnu.
  - `GET /receipts/{id}/pdf` → `FileResponse` du ticket stocké (`application/pdf`).
  - `POST /receipts/{id}/link` `{tx_key: str | None}` → `link_manual` via `write`. Réponse : la
    fiche du ticket. Débit déjà relié → 409.
  - `GET /aisles` → `{families: [{id, name, position}], aisles: [{id, store, name, family_id, lines}]}` ;
    `POST /aisles/{id}` `{family_id: int | None}` ; `POST /families` `{name}` (nom unique, sinon
    409) ; `POST /families/{id}` `{name}`.
  - `POST /upload` (multipart, `files: list[UploadFile]`) → `[FileResult]`. Chaque fichier passe par
    `ingest_file(origin='upload')` dans `write`, puis `match_receipts`. Les contrôles sont ceux de
    `/api/upload` : `%PDF-` dans les 1024 premiers octets et taille.
  - `GET /sync` → `{enabled, labels: [{label, store, last_success_at, last_attempt_at, last_error}]}` ;
    `POST /sync` → 409 si la relève est désactivée ; sinon `sync_once` dans le threadpool, puis
    `match_receipts`, et renvoie le résultat.
  - `POST /reread` → `reread` de tous les tickets puis `match_receipts` ; renvoie les compteurs.
- `_transactions.py` : `ROW_SELECT` gagne
  `(SELECT r.id FROM receipts r WHERE r.tx_key = t.tx_key AND r.link_broken = 0) AS receipt_id`,
  et `row_to_dict` renvoie `"receipt_id"`.

- [ ] **Step 1 : tests en échec** (fixture `env_courses` : app avec `start_mail_thread=False`,
  catégorie « Courses » du seed, débits insérés, tickets fictifs importés par `POST /upload`)

```python
def test_tabs_order_includes_courses(env): ...   # [..., "categories", "courses", "transactions", ...]
def test_upload_links_and_summary_counts_whole_consumption(env_courses):
    # Ticket fictif : 30,00 € = CB 18,00 + TR 12,00, avec un débit Courses de -18,00.
    # Un autre débit Courses de -24,60 n'a pas de ticket.
    s = get(client, "/api/tabs/courses/summary", **JAN)
    assert s["undetailed"] == "24.60" and s["total"] == "54.60"
    assert s["by_payment"]["meal_voucher"] == "12.00" and s["by_payment"]["card"] == "42.60"

def test_family_drilldown_products(env_courses): ...   # quantity, avg_unit et purchases attendus
def test_unsorted_aisle_counts_and_can_be_assigned(env_courses): ...
def test_manual_link_and_conflict(env_courses): ...    # 200, puis 409 sur un débit déjà relié
def test_pdf_route_serves_stored_file(env_courses): ...
def test_upload_rejects_non_pdf_and_unknown_store(env_courses): ...
def test_sync_disabled_returns_409(env_courses): ...
def test_transactions_rows_expose_receipt_id(env_courses): ...
def test_out_of_courses_flag(env_courses): ...          # débit relié classé Transport -> listé
def test_comparison_period(env_courses): ...
```

- [ ] **Step 2 : lancer** `.venv/Scripts/python -m pytest tests/test_courses_api.py tests/test_tabs_api.py -q`. Attendu : FAIL.
- [ ] **Step 3 : implémenter** `courses.py` et le câblage. `env` (dans `test_tabs_api`) passe
  `start_mail_thread=False` par défaut, via `Settings.mail=None`.
- [ ] **Step 4 : suite complète PASS**, puis commit `feat: API de l'onglet Courses et câblage des tickets`

---

### Task 6 : interface de l'onglet Courses et icône 🧾

**Files:**
- Create: `app/web/tabs/courses.js`
- Modify: `app/web/ui.js` (`transactionsTable` : icône 🧾 si `tx.receipt_id`, lien
  `#tab=courses&receipt=<id>`), `app/web/style.css`
- Test: `tests/test_web_core.py` (le module est servi et exporte `id`, `title`, `render`, comme les
  autres onglets si ce test existe déjà)

**Interfaces:**
- Consumes : routes de la Task 5, `ctx` (période, comparaison, comptes, `navigate`, `api`), helpers
  `el` et `emptyNote` de `ui.js`.
- Paramètres d'onglet : `family=<id>` (détail d'une famille), `receipt=<id>` (fiche d'un ticket).

Rendu attendu :
- **En-tête** : total, variation, répartition par moyen de paiement, et alertes cliquables (à
  relier, à ranger, erreurs).
- **Barres par famille** en CSS (pas de Chart.js), plus la ligne « Non détaillé ». Un clic ouvre
  `family`.
- **Détail d'une famille** : rayons, puis tableau des produits.
- **Liste des tickets** avec un badge de statut.
- **Fiche d'un ticket** : articles par rayon, paiements, débit relié ou liste de candidats avec un
  bouton « Relier » et un bouton « Délier », et un lien vers le PDF.
- **`<details>` « Rayons et familles »** : un select de famille par rayon, et un champ pour créer une
  famille.
- **`<details>` « Relève des tickets »** : état, erreur, bouton « Relever maintenant » caché si
  désactivé, bouton « Relire les tickets ».
- **Zone de dépôt** : `input type=file multiple accept=application/pdf`, qui envoie vers
  `/tabs/courses/upload`, puis affiche un résumé par fichier.

- [ ] **Step 1 : test du module servi** (en échec), puis implémentation.
- [ ] **Step 2 : vérification manuelle** avec le skill `run` : lancer l'app en local sur `data/dev`,
  avec les vrais tickets et un relevé réel déposé à la main par l'utilisateur s'il le souhaite.
  Vérifier :
  - l'en-tête ;
  - l'ouverture d'une famille ;
  - la fiche d'un ticket ;
  - le lien manuel ;
  - l'assignation d'un rayon ;
  - l'icône 🧾 dans Transactions.

  Faire une capture d'écran, sans la commiter.
- [ ] **Step 3 : suite complète PASS**, puis commit `feat: interface de l'onglet Courses`

---

### Task 7 : déploiement

**Files:**
- Modify: `docker-compose.yml`, `README.md`
- Test: `tests/test_constraints.py` (inchangé, doit passer), suite complète

**Interfaces:**
- `docker-compose.yml` :
  - secret `gmail_app_password: file: ./secrets/gmail_app_password.txt` ;
  - environnement :
    - `EXTRANALYZER_GMAIL_APP_PASSWORD_FILE: /run/secrets/gmail_app_password` ;
    - `EXTRANALYZER_GMAIL_USER: ${GMAIL_USER:-}` ;
    - `EXTRANALYZER_RECEIPT_LABELS: ${RECEIPT_LABELS:-Super U/Tickets de caisses=superu;intermarche/Tickets de caisses=intermarche}`.
  - Un fichier vide désactive la relève.
- README, section « Tickets de caisse » :
  - créer le mot de passe d'application (validation en deux étapes) ;
  - `printf '%s' '…' > secrets/gmail_app_password.txt`, puis `chown 10001:10001` et `chmod 400`
    comme le mot de passe actuel ;
  - `GMAIL_USER=` dans `.env` ;
  - ce que fait la relève (lecture seule, deux libellés, toutes les heures) ;
  - la seule connexion sortante est `imap.gmail.com:993` ;
  - désactiver la relève : fichier vide.

- [ ] **Step 1 : suite complète** `.venv/Scripts/python -m pytest -q`. Attendu : tout PASS. Puis
  `scripts/check_real_receipts.py data/dev`, qui doit donner 99 `ok`.
- [ ] **Step 2 : compose et README**, puis commit `docs: déploiement de la relève des tickets`.
- [ ] **Step 3 : build de l'image** `docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .`
  (route A du README) si Docker est disponible ; sinon, route B sur le Pi.
- [ ] **Step 4 : déploiement sur le Pi**, **avec l'utilisateur** : demander l'accès (SSH ou commandes
  à lui faire lancer), la route A ou B, et la création du secret et de `GMAIL_USER` sur le Pi. Puis
  `docker compose up -d`, `docker compose ps` jusqu'à « healthy », et dans l'onglet Courses
  « Relever maintenant ». Attendu : environ 99 tickets, rapprochement visible.
