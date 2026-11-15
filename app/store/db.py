import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

SCHEMA_VERSION = 5
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

# 4 -> 5 : tickets de caisse (reçus Gmail ou déposés), rayons, état de la relève
_MIGRATE_4_TO_5 = """
BEGIN;
CREATE TABLE aisle_families (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    position INTEGER NOT NULL
);

CREATE TABLE aisles (
    id INTEGER PRIMARY KEY,
    store TEXT NOT NULL,
    name TEXT NOT NULL,
    family_id INTEGER NULL REFERENCES aisle_families(id),
    UNIQUE(store, name)
);

CREATE TABLE receipts (
    id INTEGER PRIMARY KEY,
    store TEXT NOT NULL,
    stamp TEXT NULL,
    sha256 TEXT UNIQUE NOT NULL,
    stored_path TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('imap','upload')),
    message_id TEXT NULL UNIQUE,
    shop TEXT NULL,
    purchased_at TEXT NULL,
    total_cents INTEGER NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','error','pending_parser')),
    message TEXT NULL,
    tx_key TEXT NULL,
    link_mode TEXT NULL CHECK(link_mode IN ('auto','manual')),
    link_broken INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE INDEX idx_receipts_tx_key ON receipts(tx_key);

CREATE TABLE receipt_slips (
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    stored_path TEXT NOT NULL,
    stamp TEXT NULL,
    receipt_id INTEGER NULL REFERENCES receipts(id)
);

CREATE TABLE receipt_lines (
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    rank INTEGER NOT NULL,
    aisle_id INTEGER NULL REFERENCES aisles(id),
    label TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_cents INTEGER NULL,
    amount_cents INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('item','discount')),
    PRIMARY KEY(receipt_id, rank)
);

CREATE TABLE receipt_payments (
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    rank INTEGER NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('card','meal_voucher','voucher','cash','other')),
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY(receipt_id, rank)
);

CREATE TABLE mail_sync (
    label TEXT PRIMARY KEY,
    store TEXT NOT NULL,
    uidvalidity INTEGER NULL,
    last_uid INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT NULL,
    last_attempt_at TEXT NULL,
    last_error TEXT NULL
);

PRAGMA user_version = 5;
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
        if version == 4:    # 4 -> 5 : tickets de caisse
            conn.executescript(_MIGRATE_4_TO_5)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise
    return conn


def transactional(fn: Callable[[sqlite3.Connection], T]) -> Callable[[sqlite3.Connection], T]:
    """Enveloppe `fn` (qui ne valide pas) dans une transaction : validée si elle réussit, annulée sinon."""
    def run(conn: sqlite3.Connection) -> T:
        conn.execute("BEGIN")
        try:
            result = fn(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
    return run
