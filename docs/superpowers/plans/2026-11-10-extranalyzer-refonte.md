# Extranalyzer — refonte : plan de réalisation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un serveur web Python, dockerisé pour Raspberry Pi. Il importe les relevés PDF déposés, vérifie
leurs soldes, classe les transactions par règles et affiche une vue d'ensemble des dépenses sur une période
libre, dans une interface à onglets modulaires.

**Architecture :**
- Un seul processus FastAPI avec SQLite.
- Le cœur (`importer/`, `classify/`, `analytics/`, `store/`) ne connaît pas l'interface.
- Chaque onglet est un couple « module Python `app/tabs/<id>.py` + module JS `app/web/tabs/<id>.js` »,
  déclaré dans un registre unique.

**Tech Stack :**
- Python 3.12 : FastAPI, Uvicorn, python-multipart, pdfplumber, itsdangerous, sqlite3 (stdlib),
  tomllib (stdlib).
- Tests : pytest, httpx, reportlab (génération de PDF synthétiques).
- Front : JS vanilla en modules ES, Chart.js 4 (vendored).
- Docker : `python:3.12-alpine` en deux étapes.

**Spec :** `docs/superpowers/specs/2026-11-10-extranalyzer-refonte-design.md`

## Global Constraints

- **Aucune donnée réelle** des relevés dans le dépôt, les tests, les logs, les messages de commit ou la
  mémoire de l'assistant. `extraits/`, `*.xlsx`, `data/` et `.superpowers/` sont dans `.gitignore`. Les
  tests n'utilisent que des PDF synthétiques avec des libellés fictifs.
- L'interface ne charge **aucune ressource depuis internet** : Chart.js est dans `app/web/vendor/`.
  Aucune télémétrie.
- **Montants** : `Decimal` dans le code, centimes entiers (`INTEGER`) en base. Jamais de `float`.
- **Convention de signe** : montant négatif = débit, positif = crédit.
- **Date retenue pour l'analyse** : la date d'opération. La date de valeur est stockée mais non utilisée.
- **Une section qui ne se réconcilie pas** (`ouverture − Σdébits + Σcrédits ≠ clôture` au centime près)
  n'intègre **aucune** transaction.
- **Libellé sans règle** : catégorie `NULL` (« À classer »), jamais de catégorie par défaut.
- **Interface en français.** Les identifiants de code sont en anglais.
- **Compose** :
  - port publié uniquement sur `${BIND_ADDR}`, variable obligatoire, jamais `0.0.0.0` par défaut ;
  - `mem_limit: 256m`, `cpus: 1.0`, `read_only: true` ;
  - `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` ;
  - utilisateur non-root.
- **Cible matérielle** : `linux/arm64`.
- Les anciens scripts (`main.py`, `parser.py`, `Transaction.py`, `extractToExcel.py`) restent en place,
  non modifiés et non importés. Leur suppression est laissée à l'utilisateur.

## Review Focus

1. **Libellé contenant un nombre au format montant** (ex. `KARTING 19,99`) : il reste dans le libellé et
   n'est jamais pris pour le montant. Test dans la Tâche 2.
2. **Ligne datée sans montant dans les colonnes** (le montant est sur une ligne de continuation) : le
   montant est récupéré sur la continuation. S'il n'y en a pas, la réconciliation échoue proprement.
   Test dans la Tâche 3.
3. **Même PDF déposé deux fois, ou PDF différent contenant une section déjà importée** : aucun doublon de
   transactions. Test dans la Tâche 6.
4. **Fichier qui n'est pas un PDF, ou PDF d'une autre banque** : erreur claire, rien n'est importé, le
   serveur ne plante pas. Tests dans les Tâches 6 et 10.
5. **Période vide ou à cheval sur des mois partiels** : totaux à 0, séries mensuelles complètes avec des
   mois à 0, pas d'exception. Test dans la Tâche 9.

---

## Arborescence cible

```
pyproject.toml  .gitignore  Dockerfile  docker-compose.yml  .dockerignore
app/
  __init__.py  config.py  main.py  auth.py  model.py
  importer/  __init__.py base.py amounts.py creditmutuel.py validate.py service.py
  classify/  __init__.py normalize.py rules.py transfers.py seed.py rules.seed.toml
  analytics/ __init__.py periods.py totals.py recurring.py
  store/     __init__.py db.py schema.sql repo.py
  tabs/      __init__.py registry.py overview.py categories.py transactions.py epargne.py a_classer.py releves.py regles.py
  web/       index.html app.js api.js style.css vendor/chart.umd.min.js
             tabs/overview.js categories.js transactions.js epargne.js a_classer.js releves.js regles.js
tests/
  conftest.py  pdfgen.py
  test_amounts.py test_parser_basic.py test_parser_sections.py test_validate.py
  test_store.py test_import_service.py test_rules.py test_transfers.py test_analytics.py
  test_web_core.py test_tabs_api.py
scripts/check_real_statements.py
```

---

### Task 0 : socle du dépôt

**Files :**
- Create : `.gitignore`, `pyproject.toml`, `app/__init__.py`, `tests/conftest.py`

- [ ] **Step 1 :** `git init`. Créer `.gitignore` avec au minimum : `extraits/`, `*.xlsx`, `*.pdf`,
  `!tests/**/*.pdf`, `data/`, `.superpowers/`, `__pycache__/`, `.venv/`, `.pytest_cache/`.
- [ ] **Step 2 :** `pyproject.toml` (setuptools, package `app`, `requires-python = ">=3.12"`).
  - Dépendances : `fastapi`, `uvicorn`, `python-multipart`, `pdfplumber`, `itsdangerous`.
  - Extra `dev` : `pytest`, `httpx`, `reportlab`.
  - `[tool.pytest.ini_options] testpaths = ["tests"]`.
- [ ] **Step 3 :** créer un venv, puis `pip install -e .[dev]`. Lancer `pytest`.
  Attendu : `no tests ran`, code de sortie 5.
- [ ] **Step 4 :** vérifier que `git status` ne liste **aucun** fichier de `extraits/` ni `*.xlsx`.
- [ ] **Step 5 : commit** `chore: socle du projet Extranalyzer v2`

---

### Task 1 : modèle et parsing des montants

**Files :**
- Create : `app/model.py`, `app/importer/__init__.py`, `app/importer/amounts.py`
- Test : `tests/test_amounts.py`

**Interfaces — Produces :**
```python
# app/model.py
@dataclass(frozen=True)
class ParsedTransaction:
    op_date: date; value_date: date; label: str; amount: Decimal  # <0 débit, >0 crédit
@dataclass
class ParsedSection:
    account_label: str; account_number: str; statement_date: date
    opening: Decimal; closing: Decimal            # signés (DEBITEUR -> négatif)
    transactions: list[ParsedTransaction]
@dataclass
class ParsedDocument:
    statement_date: date; sections: list[ParsedSection]
class ParseError(Exception): ...
# app/importer/amounts.py
def parse_amount(text: str) -> Decimal      # "1.234,56" -> Decimal("1234.56")
def is_amount(text: str) -> bool            # regex ^\d{1,3}(?:\.\d{3})*,\d{2}$
def to_cents(d: Decimal) -> int; def from_cents(c: int) -> Decimal
```

- [ ] **Step 1 : tests**
```python
@pytest.mark.parametrize("s,v", [("2,50","2.50"),("5.686,82","5686.82"),("1.234.567,00","1234567.00")])
def test_parse_amount(s, v): assert parse_amount(s) == Decimal(v)
def test_is_amount_rejects(): assert not is_amount("12/03/2024") and not is_amount("0794") and not is_amount("2,5")
def test_cents_roundtrip(): assert from_cents(to_cents(Decimal("-1234.56"))) == Decimal("-1234.56")
def test_parse_amount_invalid(): pytest.raises(ValueError, parse_amount, "abc")
```
- [ ] **Step 2 :** `pytest tests/test_amounts.py`. Attendu : échec (ImportError).
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** `pytest tests/test_amounts.py`. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: modèle de données et parsing des montants`

---

### Task 2 : générateur de PDF synthétiques + parseur d'une section simple

**Files :**
- Create : `tests/pdfgen.py`, `app/importer/base.py`, `app/importer/creditmutuel.py`
- Test : `tests/test_parser_basic.py`

**Disposition réelle mesurée** (relevés réels, en points, origine en haut à gauche comme dans
pdfplumber) :
- page de 595 de large ;
- mots d'en-tête `Débit` (x0 ≈ 409) et `Crédit` (x0 ≈ 480), suivis chacun de `EUROS` ; certains relevés
  sont décalés de +10 ;
- montants **débit** alignés à droite à x1 ≈ `Débit.x1 + 30`, montants **crédit** à x1 ≈ `Débit.x1 + 105` ;
- nombres faisant partie du libellé : x1 ≈ `Débit.x1 − 185` ;
- dans le flux du PDF, les montants sont stockés **après** tous les libellés (ordre ≠ ordre visuel).

**Interfaces — Produces :**
```python
# tests/pdfgen.py  (fixtures uniquement, libellés fictifs)
@dataclass
class FTx:
    op: str; val: str; label: list[str]          # 1re ligne + continuations
    debit: str | None = None; credit: str | None = None
    amount_line: int = 0                          # index de la ligne de libellé portant le montant
@dataclass
class FSection:
    header: str                                   # ex. "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"
    opening: tuple[str, str, str]                 # ("CREDITEUR"|"DEBITEUR", "11/03/2024", "271,50")
    closing: tuple[str, str, str]
    txs: list[FTx]
def make_statement(path: Path, statement_date: str, sections: list[FSection], *,
                   other_accounts: list[tuple[str, str]] = (), rows_per_page: int = 25,
                   x_shift: float = 0, amounts_drawn_last: bool = True) -> Path
# app/importer/base.py
class StatementParser(Protocol):
    name: str
    def can_parse(self, pdf_path: Path) -> bool
    def parse(self, pdf_path: Path) -> ParsedDocument      # lève ParseError
# app/importer/creditmutuel.py
class CreditMutuelParser:  # implémente StatementParser, name = "creditmutuel"
```
`make_statement` dessine avec reportlab :
- en-tête « RELEVE ET INFORMATIONS BANCAIRES » et la date (ex. `10 avril 2024`) ;
- par section : la ligne d'en-tête de compte avec `Débit EUROS Crédit EUROS` aux abscisses ci-dessus,
  la ligne `Date Date valeur Opération`, puis le solde d'ouverture, les transactions et le solde de
  clôture (`SOLDE CREDITEUR AU jj/mm/aaaa` dans la colonne crédit, `DEBITEUR` dans la colonne débit) ;
- un saut de page après `rows_per_page` lignes, l'en-tête de compte et la ligne de titres étant répétés
  sur la nouvelle page ;
- puis le bloc `SITUATION DE VOS AUTRES COMPTES` avec `other_accounts`, et un pied de page `Page N`.

**Algorithme du parseur (décisions à respecter) :**
1. **Mots** : `page.extract_words()`, regroupés en lignes par `top` (tolérance 2 pt), triés par `x0`.
2. **Colonnes** : sur chaque page, repérer les mots `Débit` et `Crédit`. Un mot montant est dans la zone
   des montants si `x0 >= Débit.x0 − 40`. Il est **débit** si `x1 < Crédit.x0`, **crédit** sinon. Tout
   autre mot fait partie du libellé.
3. **Ligne de transaction** : ses deux premiers mots correspondent à `jj/mm/aaaa`.
4. **Continuation** : ligne suivante sans date, sans `SOLDE`, avec des mots situés entre la colonne date
   et la zone des montants. Ses mots de libellé s'ajoutent au libellé (séparateur : espace). Un montant
   de zone qu'elle porte devient celui de la transaction si celle-ci n'en a pas encore.
5. **Bornes** : on ignore les mots situés à gauche de la colonne de date d'opération (moins 5 pt), par
   exemple les références techniques en marge.

- [ ] **Step 1 : tests** (une section, 3 transactions, `x_shift` 0 puis 10)
```python
def test_single_section(tmp_path, x_shift):
    pdf = make_statement(tmp_path/"s.pdf", "10 avril 2024", [FSection(
        header="C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)",
        opening=("CREDITEUR","11/03/2024","100,00"), closing=("CREDITEUR","10/04/2024","1.087,50"),
        txs=[FTx("11/03/2024","11/03/2024",["PAIEMENT CB 0703 VILLEA","MAGASIN ALPHA CARTE 0000"],debit="2,50"),
             FTx("12/03/2024","12/03/2024",["VIR DE M TEST"],credit="1.000,00"),
             FTx("13/03/2024","13/03/2024",["LOISIR BETA 14/03/2024 19,99"],debit="10,00")])], x_shift=x_shift)
    doc = CreditMutuelParser().parse(pdf)
    s = doc.sections[0]
    assert doc.statement_date == date(2024,4,10)
    assert (s.account_number, s.opening, s.closing) == ("00000000001", Decimal("100.00"), Decimal("1087.50"))
    assert [t.amount for t in s.transactions] == [Decimal("-2.50"), Decimal("1000.00"), Decimal("-10.00")]
    assert s.transactions[0].label == "PAIEMENT CB 0703 VILLEA MAGASIN ALPHA CARTE 0000"
    assert s.transactions[2].label == "LOISIR BETA 14/03/2024 19,99"     # Review Focus 1
def test_can_parse_rejects_other_pdf(tmp_path): # PDF reportlab avec un texte quelconque
    assert CreditMutuelParser().can_parse(other_pdf) is False
```
- [ ] **Step 2 :** `pytest tests/test_parser_basic.py`. Attendu : échec.
- [ ] **Step 3 :** implémenter `make_statement`, puis `CreditMutuelParser`.
  - `can_parse` : la page 1 contient `RELEVE ET INFORMATIONS BANCAIRES` et les mots `Débit` et `Crédit`.
  - `parse` : lève `ParseError` si aucune section n'est trouvée.
- [ ] **Step 4 :** `pytest tests/test_parser_basic.py`. Attendu : PASS (les deux `x_shift`).
- [ ] **Step 5 : commit** `feat: parseur par coordonnées (section simple) et PDF de test synthétiques`

---

### Task 3 : plusieurs sections, plusieurs pages, cas limites

**Files :**
- Modify : `app/importer/creditmutuel.py`
- Test : `tests/test_parser_sections.py`

**Règles de découpage :**
- **Nouvelle section** : à chaque ligne d'en-tête de compte, selon la regex
  `^(?P<label>.+?) N° (?P<number>\d+) en euros` appliquée au texte de la ligne.
- **En-tête répété** : un en-tête dont le numéro est celui de la section en cours **continue** cette
  section ; il n'en crée pas de nouvelle.
- **Soldes** : le premier `SOLDE (CREDITEUR|DEBITEUR) AU` d'une section est l'ouverture, le suivant la
  clôture. Le motif peut être précédé d'autres mots sur la ligne. `DEBITEUR` donne un montant négatif.
- **Fin de lecture** : après la clôture, plus aucune ligne n'est rattachée à la section.
  `SITUATION DE VOS AUTRES COMPTES` termine la lecture du document jusqu'au prochain en-tête de compte.
- **Changement de page** : la lecture des transactions ne reprend qu'après l'en-tête de compte répété.
- **Date du relevé** : première occurrence de `\b(\d{1,2}) (janvier|février|…|décembre) (\d{4})\b`.

- [ ] **Step 1 : tests**
  - `test_two_accounts_one_pdf` : compte courant (3 tx) + Livret (1 tx, crédit). Attendu : 2 sections,
    avec les bons numéros et montants.
  - `test_multipage_section` : 60 transactions, `rows_per_page=25`. Attendu : 1 section, 60 tx, dans
    l'ordre.
  - `test_amount_on_continuation_line` : `FTx(..., label=["VIR SEPA X","MOTIF Y"], debit="30,00",
    amount_line=1)`. Attendu : montant −30,00. **(Review Focus 2)**
  - `test_debiteur_balance` : ouverture `("DEBITEUR", …, "50,00")`. Attendu : `opening == Decimal("-50.00")`.
  - `test_empty_section` : aucune tx. Attendu : section avec `transactions == []`.
  - `test_other_accounts_block_ignored` : `other_accounts=[("00000000003 LIVRET TEST","+1.674,99")]`.
    Attendu : ni transaction ni section créée pour ce bloc.
  - `test_amounts_not_drawn_last` : `amounts_drawn_last=False`. Attendu : même résultat
    (indépendance vis-à-vis de l'ordre du flux).
- [ ] **Step 2 :** lancer les tests. Attendu : échecs.
- [ ] **Step 3 :** implémenter les règles ci-dessus.
- [ ] **Step 4 :** `pytest tests/test_parser_basic.py tests/test_parser_sections.py`. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: parseur multi-comptes, multi-pages et cas limites`

---

### Task 4 : réconciliation

**Files :**
- Create : `app/importer/validate.py`
- Test : `tests/test_validate.py`

**Interfaces — Produces :**
`def reconciliation_gap(section: ParsedSection) -> Decimal`, qui renvoie
`closing − (opening + Σ amounts)` ; `Decimal("0.00")` signifie OK.

- [ ] **Step 1 : tests**
  - section cohérente : gap = 0 ;
  - un montant manquant (2,50) : gap = −2,50 ;
  - section vide avec ouverture = clôture : gap = 0.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: contrôle de réconciliation des soldes`

---

### Task 5 : stockage SQLite

**Files :**
- Create : `app/store/__init__.py`, `app/store/db.py`, `app/store/schema.sql`, `app/store/repo.py`
- Test : `tests/test_store.py`

**Schéma** (`schema.sql`, versionné par `PRAGMA user_version = 1`) :
```sql
accounts(id INTEGER PK, number TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
         kind TEXT NOT NULL CHECK(kind IN ('checking','savings','exchange','wallet')),
         source TEXT NOT NULL DEFAULT 'pdf')
documents(id PK, sha256 TEXT UNIQUE NOT NULL, filename TEXT, stored_path TEXT NOT NULL,
          parser TEXT NOT NULL, statement_date TEXT, imported_at TEXT NOT NULL)
sections(id PK, document_id FK, account_id FK, statement_date TEXT NOT NULL,
         opening_cents INT, closing_cents INT, gap_cents INT NOT NULL,
         status TEXT NOT NULL CHECK(status IN ('ok','error','conflict','superseded')),
         content_hash TEXT NOT NULL)
categories(id PK, name TEXT NOT NULL, parent_id INT NULL REFERENCES categories(id),
           kind TEXT NOT NULL CHECK(kind IN ('expense','income','savings')), UNIQUE(name, parent_id))
rules(id PK, pattern TEXT NOT NULL, match TEXT NOT NULL CHECK(match IN ('contains','startswith','regex')),
      direction TEXT NULL CHECK(direction IN ('debit','credit')), account_id INT NULL,
      category_id INT NOT NULL, priority INT NOT NULL)
transactions(id PK, section_id FK, account_id FK, tx_key TEXT UNIQUE NOT NULL,
             op_date TEXT NOT NULL, value_date TEXT, label TEXT NOT NULL, label_norm TEXT NOT NULL,
             amount_cents INT NOT NULL, category_id INT NULL, rule_id INT NULL,
             manual INT NOT NULL DEFAULT 0, mirror_of INT NULL REFERENCES transactions(id))
manual_overrides(tx_key TEXT PK, category_id INT NOT NULL)
```
Index sur `transactions(op_date)`, `transactions(category_id)`, `transactions(account_id)`.

**Type de compte** : `kind` vaut `savings` si le libellé du compte contient `LIVRET` ou `EPARGNE`, et
`checking` sinon.

**Interfaces — Produces :**
```python
def connect(path: Path) -> sqlite3.Connection   # crée/migre le schéma, foreign_keys=ON, WAL
# repo.py : fonctions fines, une par requête, ex.
def upsert_account(conn, number: str, label: str) -> int
def account_kind_from_label(label: str) -> str
```

- [ ] **Step 1 : tests**
  - `connect` deux fois sur le même fichier : `user_version == 1`, pas d'erreur ;
  - `upsert_account` est idempotent ;
  - `account_kind_from_label("LIVRET BLEU") == "savings"` ;
  - insertion d'un montant via `to_cents`, puis relecture exacte.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: schéma SQLite et accès aux données`

---

### Task 6 : service d'import

**Files :**
- Create : `app/importer/service.py`
- Test : `tests/test_import_service.py`

**Interfaces :**
- Consumes : `StatementParser`, `reconciliation_gap`, `connect`/`repo`, et `apply_rules` (Tâche 7) via
  un paramètre injecté.
- Produces :
```python
@dataclass
class SectionReport: account_number: str; statement_date: date; status: str; gap: Decimal; tx_count: int
@dataclass
class ImportReport: document_id: int | None; duplicate_file: bool; sections: list[SectionReport]; error: str | None
def import_pdf(conn, pdf_bytes: bytes, filename: str, pdf_dir: Path,
               parsers: list[StatementParser], on_new_transactions: Callable[[sqlite3.Connection, list[int]], None]) -> ImportReport
def resolve_conflict(conn, section_id: int, accept: bool, on_new_transactions) -> None
def reanalyse_all(conn, pdf_dir: Path, parsers, on_new_transactions) -> list[ImportReport]
def tx_key(account_number: str, op_date: date, amount: Decimal, label_norm: str, rank: int) -> str  # sha1 hex
```

**Comportement :**
1. **Fichier déjà connu** : si le `sha256` des octets existe déjà →
   `ImportReport(duplicate_file=True, sections=[])`, sans aucune écriture.
2. **Aucun parseur reconnu** (`can_parse` faux partout) ou `ParseError` → `error` renseigné, rien
   n'est écrit. Le PDF n'est pas conservé.
3. **Sinon** : le PDF est écrit dans `pdf_dir/<sha256>.pdf`, puis une ligne est créée dans `documents`.
   Pour chaque section :
   - `content_hash` = sha1 des transactions et des soldes ;
   - **gap ≠ 0** → `status='error'`, aucune transaction n'est insérée ;
   - **section déjà présente en `ok`** (même compte, même `statement_date`) avec le même `content_hash`
     → `status='superseded'`, rien n'est inséré ;
   - **même cas mais `content_hash` différent** → `status='conflict'`, rien n'est inséré, en attendant
     `resolve_conflict` ;
   - **sinon** → `status='ok'`, les transactions sont insérées avec leur `tx_key`. `rank` est le rang
     parmi les doublons exacts de la même section. Si `tx_key` est présent dans `manual_overrides`, la
     catégorie et `manual=1` sont réappliqués. On appelle ensuite
     `on_new_transactions(conn, new_ids)`.
4. **`resolve_conflict(accept=True)`** : l'ancienne section passe en `superseded`, ses transactions sont
   supprimées, puis la nouvelle est insérée comme au point 3. Les `manual_overrides` sont conservés.
   **`accept=False`** : la nouvelle section passe en `superseded`.
5. **`reanalyse_all`** : re-parse chaque PDF conservé.
   - Si une section `ok` échoue désormais : on garde l'ancienne version et on renvoie le rapport en
     `error`.
   - Sinon : on remplace les transactions, et les `manual_overrides` s'appliquent.
6. Toute l'écriture d'un import se fait dans **une seule transaction SQLite**.

- [ ] **Step 1 : tests**
  - `test_import_ok` : 2 sections, statut `ok` toutes les deux, nombre de tx correct, PDF présent dans
    `pdf_dir`.
  - `test_same_file_twice` : le 2ᵉ import renvoie `duplicate_file=True`, aucune tx de plus.
    **(Review Focus 3)**
  - `test_overlapping_section_identical` : un PDF différent contient la même section → `superseded`,
    pas de doublon. **(Review Focus 3)**
  - `test_overlapping_section_different` → `conflict`. Puis `resolve_conflict(accept=True)` remplace les
    transactions.
  - `test_unbalanced_section_rejected` : une section a un montant faux → `error`, gap correct, 0 tx pour
    elle, l'autre section en `ok`.
  - `test_not_a_pdf` (`b"hello"`) et `test_unknown_pdf` : `error` renseigné, aucune ligne dans
    `documents`, aucun fichier écrit. **(Review Focus 4)**
  - `test_manual_override_survives_reanalyse`.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: service d'import avec dédoublonnage, conflits et ré-analyse`

---

### Task 7 : normalisation des libellés et moteur de règles

**Files :**
- Create : `app/classify/__init__.py`, `normalize.py`, `rules.py`, `seed.py`, `rules.seed.toml`
- Modify : `app/importer/service.py` (remplir `label_norm` via `normalize_label`)
- Test : `tests/test_rules.py`

**Interfaces — Produces :**
```python
def normalize_label(label: str) -> str
def default_priority(pattern: str, match: str, direction: str | None, account_id: int | None) -> int
def apply_rules(conn, tx_ids: list[int] | None = None) -> int          # None = toutes ; renvoie nb classées
def create_rule(conn, pattern, match, category_id, direction=None, account_id=None, priority=None,
                apply_to: Literal["unclassified", "all_non_manual"] = "unclassified") -> int
def update_rule(conn, rule_id: int, **fields) -> None    # reclasse les tx non manuelles concernées
def delete_rule(conn, rule_id: int) -> None              # tx non manuelles de cette règle -> NULL puis apply_rules
def set_manual_category(conn, tx_id: int, category_id: int | None) -> None  # écrit manual_overrides
def load_seed(conn, path: Path) -> None                  # catégories + règles, idempotent
```

**Décisions :**
- `normalize_label` :
  - met en majuscules ;
  - retire `CARTE \d{4}`, les dates `jj/mm/aaaa`, les blocs `\b\d{4}\b` qui suivent
    `PAIEMENT (CB|PSC|MOB)`, et les références alphanumériques de 8 caractères ou plus contenant au
    moins 4 chiffres ;
  - compresse les espaces.
- **Correspondance** : elle porte sur `label_norm`. `contains` et `startswith` ignorent la casse.
- **Priorité par défaut** = `10 × len(pattern)` + 5 si `direction` + 5 si `account_id` + 3 si
  `startswith`. La plus haute gagne ; à égalité, la règle la plus ancienne gagne.
- **Transactions manuelles** : `manual=1` → jamais touchées par les règles.
- **Sens et virements** : une règle sans `direction` s'applique aux deux sens. C'est ce qui fait qu'un
  remboursement tombe dans la catégorie de dépense (spec § 5.4 bis).
- `rules.seed.toml` contient **uniquement** l'arbre des catégories proposé :
  - **Dépenses** : Courses, Restauration, Transport (Carburant, Train, Covoiturage, Péage/Parking),
    Logement, Factures & abonnements (Téléphonie/Internet, Streaming, Énergie), Santé, Loisirs, Shopping,
    Cadeaux, Frais bancaires, Impôts & taxes, Divers ;
  - **Revenus** : Salaire, Remboursements de proches, Autres revenus ;
  - **Épargne** : Livret Bleu, Livret Jeune, CEL, Kraken, Intérêts.

  Plus quelques règles **génériques** sans données personnelles, par exemple `INTERETS` → Épargne/Intérêts
  et `F COTIS` → Frais bancaires.

- [ ] **Step 1 : tests**
  - `normalize_label("PAIEMENT CB 1203 VILLEA MAGASIN ALPHA CARTE 0000") == "PAIEMENT CB VILLEA MAGASIN ALPHA"` ;
  - une règle plus longue l'emporte sur une plus courte ;
  - la `direction` est respectée ;
  - un remboursement (crédit) classé en Courses par une règle sans direction ;
  - une transaction `manual=1` n'est pas modifiée par `apply_rules` ;
  - `update_rule` (changement de catégorie) reclasse les transactions non manuelles de la règle ;
  - `delete_rule` → transactions sans autre règle repassent à `NULL` ;
  - `load_seed` appelé deux fois → pas de doublon ;
  - un libellé sans règle reste `NULL`.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: normalisation des libellés et moteur de règles`
- [ ] **Step 6 : point de contrôle utilisateur.** Montrer l'arbre de catégories proposé (pas de données
  réelles) et attendre sa validation ou ses retouches avant de continuer.

---

### Task 8 : appariement des virements internes

**Files :**
- Create : `app/classify/transfers.py`
- Test : `tests/test_transfers.py`

**Interfaces — Produces :** `def pair_internal_transfers(conn) -> int` (nombre de paires créées). Elle
est appelée après chaque import réussi, dans le même `on_new_transactions`.

**Décision :**
- **Critères d'une paire** :
  - deux transactions sur deux comptes **différents** de l'utilisateur ;
  - montants opposés exacts ;
  - `|op_date1 − op_date2| ≤ 5 jours` ;
  - l'une des deux sur un compte `savings`.
- La transaction côté compte `savings` reçoit `mirror_of = id` de l'autre, et la même catégorie Épargne.
- Plusieurs candidats → on prend l'écart de dates minimal, puis le plus petit id.
- Une transaction déjà appariée n'est jamais réappariée.

- [ ] **Step 1 : tests**
  - −100 sur le compte courant le 12/03, +100 sur le Livret le 13/03 → 1 paire, `mirror_of` côté Livret ;
  - deux virements identiques le même jour → 2 paires distinctes ;
  - 8 jours d'écart → pas de paire ;
  - deux comptes `checking` → pas de paire.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: appariement des virements internes`

---

### Task 9 : calculs d'analyse

**Files :**
- Create : `app/analytics/__init__.py`, `periods.py`, `totals.py`, `recurring.py`
- Test : `tests/test_analytics.py`

**Interfaces — Produces :**
```python
@dataclass(frozen=True)
class Period: start: date; end: date      # bornes incluses
def previous_period(p: Period) -> Period  # même durée, juste avant
@dataclass
class Totals: expenses: Decimal; income: Decimal; savings_net: Decimal; balance: Decimal
              unclassified_debits: Decimal; unclassified_credits: Decimal
def totals(conn, p: Period, account_ids: list[int] | None) -> Totals
def by_category(conn, p, account_ids, kind: str, level: Literal["parent","child"], parent_id: int | None = None) -> list[tuple[int|None, str, Decimal]]
def monthly_series(conn, p, account_ids) -> list[dict]   # {"month":"2025-01","expenses":…,"income":…,"savings_net":…,"by_category":{name: amount}}
def top_increases(conn, cur: Period, ref: Period, account_ids, n: int = 5) -> list[tuple[str, Decimal]]
def recurring(conn, p: Period, account_ids) -> list[dict]  # {"label_norm", "avg_amount", "months"}
```
`account_ids=None` désigne les comptes `checking` (vue par défaut, spec § 5.5).

**Définitions (spec § 5.4 bis, à appliquer telles quelles) :**
- Les transactions avec `mirror_of IS NOT NULL` sont **exclues** de tous les calculs.
- `expenses` = −Σ montant des transactions dont la catégorie (ou son parent) est `expense`. C'est un
  **net** : les remboursements réduisent le total. Les débits non classés y sont **ajoutés**, et exposés
  à part dans `unclassified_debits`.
- `income` = Σ montant des catégories `income`, plus les crédits non classés (exposés dans
  `unclassified_credits`).
- `savings_net` = Σ des transactions `savings` :
  - **−montant** pour une transaction sur un compte `checking` (versement +, retrait −) ;
  - **+montant** pour une transaction sur un compte `savings` (intérêts, versements reçus).
- `balance` = `income − expenses − savings_net`.
- `monthly_series` couvre **tous** les mois de la période, y compris ceux à 0.
- `recurring` : même `label_norm` sur au moins 3 mois calendaires consécutifs de la période, chaque
  montant à ±10 % de la moyenne, débits uniquement.

- [ ] **Step 1 : tests**
  - jeu de données construit en base (fictif) : salaire +1 500, courses −100, remboursement courses +30,
    virement −200 vers le Livret (apparié) et intérêts +5 sur le Livret. Avec les comptes
    `[courant, livret]` : `expenses == 70`, `income == 1500`, `savings_net == 205`,
    `balance == 1225`. Avec la vue par défaut : `savings_net == 200`.
  - `previous_period(Period(2025-01-01, 2025-03-31))` == `Period(2024-10-03, 2024-12-31)` (même nombre
    de jours) ;
  - **période vide** : tous les totaux à 0 ;
  - `monthly_series` du 15/01 au 10/03 renvoie 3 mois, dont un mois sans transaction à 0
    **(Review Focus 5)** ;
  - `recurring` détecte 5,99 sur 3 mois et ignore 2 mois.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter avec des requêtes SQL agrégées, et convertir en `Decimal` à la sortie.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: calculs d'analyse (totaux, catégories, séries, récurrents)`

---

### Task 10 : cœur web — config, authentification, registre d'onglets, dépôt

**Files :**
- Create : `app/config.py`, `app/auth.py`, `app/tabs/__init__.py`, `app/tabs/registry.py`, `app/main.py`,
  `app/web/index.html`, `app/web/app.js`, `app/web/api.js`, `app/web/style.css`,
  `app/web/vendor/chart.umd.min.js`
- Test : `tests/test_web_core.py`

**Interfaces — Produces :**
```python
# config.py — variables d'environnement
@dataclass(frozen=True)
class Settings: data_dir: Path; password_hash: bytes; max_upload_mb: int = 20
def load_settings() -> Settings
# EXTRANALYZER_DATA_DIR (défaut /data), EXTRANALYZER_PASSWORD ou EXTRANALYZER_PASSWORD_FILE, EXTRANALYZER_MAX_UPLOAD_MB
# hash via hashlib.scrypt (sel aléatoire au démarrage) ; comparaison hmac.compare_digest
# tabs/registry.py
@dataclass(frozen=True)
class Tab: id: str; title: str; order: int; router: APIRouter
# tabs/__init__.py — LA liste à éditer pour ajouter/retirer un onglet :
ENABLED_TABS: list[str] = ["overview", "categories", "transactions", "epargne", "a_classer", "releves", "regles"]
def load_tabs(enabled: list[str] = ENABLED_TABS) -> list[Tab]   # importlib "app.tabs.<id>", lit TAB
# main.py
def create_app(settings: Settings, enabled_tabs: list[str] | None = None) -> FastAPI
```

**Routes du cœur :**
- `POST /api/login {password}` : en cas de succès, cookie de session signé (itsdangerous, clé dans
  `data_dir/secret.key`, créée en `0600` si absente), `HttpOnly`, `SameSite=Strict`.
- **Limitation** : 5 échecs par minute et par IP → `429`.
- `POST /api/logout`.
- `GET /api/tabs` → `[{id, title, order}]`.
- `GET /api/meta` → `{accounts:[{id,label,kind}], categories:[…arbre…], unclassified_count, data_range:{min,max}}`.
- `POST /api/upload` (multipart, champ `file`) → `ImportReport` en JSON.
  - Taille maximale `max_upload_mb`, sinon `413`.
  - L'import tourne dans un thread (`run_in_threadpool`).
- `POST /api/sections/{id}/resolve {accept}`.
- `POST /api/reanalyse`.
- **Statique** : `/` sert `index.html`, `/static/*` sert `app/web/`.
- **Protections** : toute route `/api/*` sauf `login` exige la session (`401` sinon). Toute méthode
  autre que GET exige en plus l'en-tête `X-Extranalyzer: 1` (`403` sinon).
- **Au démarrage** : `connect`, puis `load_seed`.

**Front du cœur :**
- `api.js` exporte `api.get(path, params)` et `api.post(path, body | FormData)` (ajoute l'en-tête,
  redirige vers l'écran de connexion sur `401`).
- `app.js` :
  - formulaire de connexion ;
  - barre commune : période (deux `<input type=date>` et raccourcis Mois, Trimestre, Année, Tout),
    comparaison (aucune, période précédente, même période N−1), sélecteur de comptes, bouton
    « Déposer un relevé » (le rapport d'import s'affiche en toast), pastille « À classer (n) » ;
  - construit les onglets depuis `/api/tabs`, puis `import("./tabs/" + id + ".js")` ;
  - appelle `module.render(container, ctx)` où `ctx = {period, compare, accountIds, api, navigate(tabId, params), refresh()}` ;
  - la période est mémorisée dans l'URL (hash) ;
  - thème clair et sombre via `prefers-color-scheme`.

- [ ] **Step 1 : tests** (httpx `TestClient`, `data_dir` temporaire)
  - sans session → `/api/tabs` renvoie `401` ;
  - mauvais mot de passe 6 fois → `429` ;
  - bon mot de passe → `/api/tabs` liste les onglets dans l'ordre ;
  - `create_app(..., enabled_tabs=["overview"])` → un seul onglet (**modularité**) ;
  - un module d'onglet factice injecté via `enabled_tabs` est servi ;
  - POST sans l'en-tête `X-Extranalyzer` → `403` ;
  - upload d'un PDF synthétique → rapport `ok`. Upload de `b"hello"` → `200` avec `error` renseigné, et
    le serveur reste fonctionnel **(Review Focus 4)** ;
  - upload au-delà de la limite → `413`.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter. Télécharger Chart.js 4.4.x (UMD minifié) dans `app/web/vendor/` en
  vérifiant le SHA-256 publié par le paquet npm.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS.
- [ ] **Step 5 : commit** `feat: cœur web (auth, registre d'onglets, dépôt de relevés)`

---

### Task 11 : onglets Vue d'ensemble, Catégories, Transactions, Épargne

**Files :**
- Create : `app/tabs/{overview,categories,transactions,epargne}.py`,
  `app/web/tabs/{overview,categories,transactions,epargne}.js`
- Test : `tests/test_tabs_api.py`

**API par onglet** (toutes acceptent `start`, `end`, `accounts` (ids séparés par des virgules,
optionnel), `cstart`, `cend` (comparaison, optionnel)) :
- **`overview`** — `GET /api/tabs/overview/summary` renvoie :
  `{totals, compare_totals|null, by_category:[{id,name,amount}], monthly:[…], top_increases:[…], recurring:[…]}`.
- **`categories`** :
  - `GET /api/tabs/categories/tree` (montants par parent) ;
  - `GET /api/tabs/categories/{id}` → `{children:[…], monthly:[…], transactions:[…]}`.
- **`transactions`** :
  - `GET /api/tabs/transactions/list?q=&category=&sort=&page=` (50 par page) ;
  - `POST /api/tabs/transactions/{id}/category {category_id}` → `set_manual_category`.
- **`epargne`** — `GET /api/tabs/epargne/summary` → par destination :
  `{deposits, withdrawals, net}`, avec une série mensuelle cumulée.

**Front :** reproduit la maquette validée (spec § 5.6).
- **Vue d'ensemble** : 4 tuiles (Dépenses, Revenus, Épargne, Solde de la période), avec la variation
  quand une comparaison est choisie et la mention « dont à classer » ; camembert ; barres par catégorie
  (un clic ouvre `navigate("categories", {id})`) ; barres empilées mensuelles avec une courbe d'épargne ;
  listes « Plus grosses hausses » et « Dépenses récurrentes ».
- **Catégories** : liste à gauche, détail à droite, reclassement sur place.

- [ ] **Step 1 : tests API** sur le jeu de données de la Tâche 9 :
  - `summary` renvoie les totaux attendus ;
  - `categories/{id}` renvoie les sous-catégories ;
  - le reclassement manuel modifie `by_category` et crée un `manual_override` ;
  - `start > end` → `422`.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter les modules Python, puis JS.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS. Puis lancer l'application en local
  (`uvicorn app.main:app` avec un `data_dir` temporaire), déposer 2 PDF synthétiques et vérifier
  visuellement chaque onglet dans le navigateur (thème clair et sombre, largeur mobile 375 px sans
  défilement horizontal).
- [ ] **Step 5 : commit** `feat: onglets vue d'ensemble, catégories, transactions, épargne`

---

### Task 12 : onglets À classer, Relevés, Règles

**Files :**
- Create : `app/tabs/{a_classer,releves,regles}.py`, `app/web/tabs/{a_classer,releves,regles}.js`
- Modify : `tests/test_tabs_api.py`

**API :**
- **`a_classer`** :
  - `GET /api/tabs/a_classer/list` : transactions `category_id IS NULL`, regroupées par `label_norm`,
    avec leur nombre et leur total ;
  - `POST /api/tabs/a_classer/classify {label_norm, category_id, create_rule: bool, pattern?, match?, direction?}`.
    Avec `create_rule`, on appelle `create_rule(..., apply_to="unclassified")`. Sinon, on applique
    `set_manual_category` à chaque transaction du groupe.
- **`releves`** :
  - `GET /api/tabs/releves/list` : documents et sections, avec statut, écart et nombre de transactions ;
  - les actions passent par les routes du cœur `resolve` et `reanalyse`.
- **`regles`** : `GET` (liste) ; `POST` / `PATCH /{id}` / `DELETE /{id}`, qui appellent
  `create_rule`, `update_rule` et `delete_rule` ; `POST /test {pattern, match}` renvoie les 20 premiers
  libellés correspondants (aperçu).

**Front :**
- **À classer** : une ligne par groupe de libellés, avec deux listes déroulantes (catégorie →
  sous-catégorie), une case « créer une règle » cochée par défaut et un motif prérempli avec
  `label_norm`, modifiable.
- **Relevés** : statuts en couleur ; bouton « Remplacer / Garder l'ancien » sur les `conflict` ; écart
  affiché sur les `error`.
- **Règles** : tableau modifiable, avec un aperçu des correspondances.

- [ ] **Step 1 : tests API**
  - classer un groupe en créant une règle → le groupe disparaît de la liste, `unclassified_count`
    diminue ;
  - la section `conflict` apparaît dans `releves/list` ;
  - `regles/test` renvoie les correspondances ;
  - un PATCH de règle reclasse les transactions.
- [ ] **Step 2 :** lancer les tests. Attendu : échec.
- [ ] **Step 3 :** implémenter.
- [ ] **Step 4 :** lancer les tests. Attendu : PASS. Vérification visuelle comme à la Tâche 11.
- [ ] **Step 5 : commit** `feat: onglets à classer, relevés, règles`

---

### Task 13 : Docker minimal pour Raspberry Pi

**Files :**
- Create : `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md` (déploiement)

**Décisions :**
- **Étape `build`** : `python:3.12-alpine` ; `pip wheel --only-binary=:all: --wheel-dir /w .` (refuse
  toute compilation), puis `pip install --no-compile --prefix=/install /w/*.whl`.
- **Étape finale** : `python:3.12-alpine` ;
  - copier `/install` et `app/` ;
  - `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` ;
  - `adduser -D -u 10001 app` et `USER app` ;
  - `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1","--no-access-log"]`
    (dans le conteneur ; l'exposition réelle est filtrée par le compose) ;
  - `HEALTHCHECK` en Python stdlib (`urllib`) sur `GET /healthz`, à ajouter dans `main.py` sans
    authentification ; il renvoie `{"ok":true}`.
- **Repli** : si l'étape `build` échoue faute de wheel musllinux aarch64 pour une dépendance, passer les
  deux étapes sur `python:3.12-slim` et le noter dans le README.
- **Compose** (service unique `extranalyzer`) :
  - `ports: ["${BIND_ADDR:?BIND_ADDR requis (IP WireGuard/LAN du Pi)}:8000:8000"]` ;
  - `volumes: [data:/data]`, `tmpfs: [/tmp]` ;
  - `secrets: [password]` (fichier `./secrets/password.txt`, ignoré par git), avec
    `EXTRANALYZER_PASSWORD_FILE=/run/secrets/password` ;
  - `read_only: true`, `mem_limit: 256m`, `cpus: 1.0`, `restart: unless-stopped` ;
  - `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `logging` json-file `max-size: 1m`.
- `.dockerignore` : tout sauf `app/`, `pyproject.toml`. En particulier `extraits/`, `data/`, `tests/`.

- [ ] **Step 1 :** `docker buildx build --platform linux/arm64 -t extranalyzer:arm64 --load .`
  Attendu : succès. Sinon, appliquer le repli.
- [ ] **Step 2 :** `docker image ls extranalyzer` : noter la taille dans le README. Attendu :
  < 200 Mo sur Alpine.
- [ ] **Step 3 :** `docker compose up -d` avec `BIND_ADDR=127.0.0.1` sur la machine de dev (image amd64
  ou émulation). Attendu : healthcheck `healthy`, connexion et dépôt d'un PDF synthétique OK.
  `docker stats` : mémoire au repos < 120 Mo.
- [ ] **Step 4 :** `docker compose config` sans `BIND_ADDR`. Attendu : erreur explicite.
- [ ] **Step 5 :** README : build sur le PC ou sur le Pi, création du secret, `BIND_ADDR` = IP WireGuard,
  sauvegarde du volume `data`.
- [ ] **Step 6 : commit** `feat: image Docker Alpine multi-stage et compose durci`

---

### Task 14 : vérification sur les relevés réels (non commitée en sortie)

**Files :**
- Create : `scripts/check_real_statements.py`

**Interface :** `python scripts/check_real_statements.py <dossier>`
- importe chaque PDF dans une base **temporaire** (supprimée à la fin) ;
- affiche **uniquement** des compteurs : nombre de PDF, de sections `ok`, `error`, `superseded` et
  `conflict`, de transactions, de transactions à classer, de paires de virements internes ;
- n'affiche aucun libellé, montant ni numéro de compte.

- [ ] **Step 1 :** écrire le script.
- [ ] **Step 2 :** le lancer sur `extraits/`. Attendu : **0 section `error`**. Sinon, diagnostiquer avec
  superpowers:systematic-debugging en n'affichant que des compteurs ou positions, puis corriger le
  parseur en ajoutant d'abord un test synthétique qui reproduit le cas.
- [ ] **Step 3 : commit** du script seul : `chore: script de vérification sur relevés réels`.
- [ ] **Step 4 : point de contrôle utilisateur.**
  - L'utilisateur déploie, importe ses relevés via le panneau, puis tranche lui-même les libellés de
    l'onglet « À classer ».
  - Pour les libellés douteux, l'assistant **demande** au lieu de deviner, et n'enregistre rien en
    mémoire persistante.
