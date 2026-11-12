import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 6
BUSY_TIMEOUT_S = 30     # une écriture d'onglet attend la fin d'une ré-analyse au lieu d'échouer

_MIGRATE_2_TO_3 = """
BEGIN;
ALTER TABLE transactions ADD COLUMN classified_by TEXT NULL
    CHECK(classified_by IN ('manual','rule','pair','model'));
ALTER TABLE transactions ADD COLUMN confidence REAL NULL;
ALTER TABLE transactions ADD COLUMN suggested_category_id INTEGER NULL REFERENCES categories(id);
ALTER TABLE transactions ADD COLUMN suggestion_reason TEXT NULL;
UPDATE transactions SET classified_by = 'manual' WHERE manual = 1;
UPDATE transactions SET classified_by = 'rule' WHERE manual = 0 AND rule_id IS NOT NULL;
UPDATE transactions SET classified_by = 'pair' WHERE mirror_of IS NOT NULL
    AND (SELECT s.category_id FROM transactions s WHERE s.id = transactions.mirror_of) IS NOT NULL;
PRAGMA user_version = 3;
COMMIT;
"""

# 3 -> 4 : nombre d'opérations similaires derrière une suggestion (affiché et pris en compte par la validation groupée)
_MIGRATE_3_TO_4 = """
BEGIN;
ALTER TABLE transactions ADD COLUMN suggestion_support INTEGER NULL;
PRAGMA user_version = 4;
COMMIT;
"""

# 4 -> 5 : catégories du seed supprimées ou renommées par l'utilisateur (le seed ne les recrée pas)
_MIGRATE_4_TO_5 = """
BEGIN;
CREATE TABLE IF NOT EXISTS seed_dismissed (
    name TEXT NOT NULL,
    parent_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(name, parent_name)
);
PRAGMA user_version = 5;
COMMIT;
"""


# 5 -> 6 : sources externes (Enable Banking) et origine des opérations
_MIGRATE_5_TO_6 = """
BEGIN;
ALTER TABLE transactions ADD COLUMN source TEXT NOT NULL DEFAULT 'pdf';
CREATE TABLE bank_links (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    session_id TEXT UNIQUE NOT NULL,
    aspsp_name TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','expired','replaced','revoked')),
    created_at TEXT NOT NULL,
    last_error TEXT,
    retry_after TEXT
);

CREATE TABLE bank_auth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE external_accounts (
    id INTEGER PRIMARY KEY,
    link_id INTEGER NOT NULL REFERENCES bank_links(id),
    uid TEXT NOT NULL,
    identification_hash TEXT,
    iban TEXT,
    name TEXT,
    currency TEXT,
    cash_account_type TEXT,
    account_id INTEGER NULL REFERENCES accounts(id),
    ignored INTEGER NOT NULL DEFAULT 0,
    last_sync_at TEXT,
    balance_cents INTEGER,
    balance_date TEXT,
    balance_gap_cents INTEGER,
    accepted_gap_cents INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE external_transactions (
    transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
    raw_json TEXT NOT NULL
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,
    added INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE INDEX idx_transactions_source_account ON transactions(source, account_id, op_date);
PRAGMA user_version = 6;
COMMIT;
"""


def connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Ouvre la base ; crée le schéma si user_version vaut 0, migre une base plus ancienne.

    `check_same_thread=False` : connexion ouverte dans un thread et utilisée (séquentiellement) dans un
    autre, comme une dépendance FastAPI par requête.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"schéma de base inconnu (user_version={version} > {SCHEMA_VERSION})")
    if version == 0:
        schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
        return conn
    try:
        if version == 1:    # 1 -> 2 : message persistant d'une section (ex. collision d'identités)
            conn.executescript("BEGIN; ALTER TABLE sections ADD COLUMN message TEXT; PRAGMA user_version = 2; COMMIT;")
            version = 2
        if version == 2:    # 2 -> 3 : origine et confiance du classement
            conn.executescript(_MIGRATE_2_TO_3)
            version = 3
        if version == 3:    # 3 -> 4 : nombre d'opérations similaires d'une suggestion
            conn.executescript(_MIGRATE_3_TO_4)
            version = 4
        if version == 4:    # 4 -> 5 : catégories du seed écartées par l'utilisateur
            conn.executescript(_MIGRATE_4_TO_5)
            version = 5
        if version == 5:    # 5 -> 6 : sources externes (Enable Banking)
            conn.executescript(_MIGRATE_5_TO_6)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise
    return conn
