BEGIN;

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    number TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('checking','savings','exchange','wallet')),
    source TEXT NOT NULL DEFAULT 'pdf'
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT,
    stored_path TEXT NOT NULL,
    parser TEXT NOT NULL,
    statement_date TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE sections (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    statement_date TEXT NOT NULL,
    opening_cents INTEGER,
    closing_cents INTEGER,
    gap_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','error','conflict','superseded')),
    content_hash TEXT NOT NULL,
    message TEXT
);

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER NULL REFERENCES categories(id),
    kind TEXT NOT NULL CHECK(kind IN ('expense','income','savings')),
    UNIQUE(name, parent_id)
);

CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    match TEXT NOT NULL CHECK(match IN ('contains','startswith','regex')),
    direction TEXT NULL CHECK(direction IN ('debit','credit')),
    account_id INTEGER NULL REFERENCES accounts(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    priority INTEGER NOT NULL
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    -- nullable: future non-PDF sources (exchange/wallet) have no section
    section_id INTEGER REFERENCES sections(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    tx_key TEXT UNIQUE NOT NULL,
    op_date TEXT NOT NULL,
    value_date TEXT,
    label TEXT NOT NULL,
    label_norm TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    category_id INTEGER NULL REFERENCES categories(id),
    rule_id INTEGER NULL REFERENCES rules(id),
    manual INTEGER NOT NULL DEFAULT 0,
    mirror_of INTEGER NULL REFERENCES transactions(id),
    classified_by TEXT NULL CHECK(classified_by IN ('manual','rule','pair','model')),
    confidence REAL NULL,
    suggested_category_id INTEGER NULL REFERENCES categories(id),
    suggestion_reason TEXT NULL,
    suggestion_support INTEGER NULL,
    source TEXT NOT NULL DEFAULT 'pdf'
);

CREATE TABLE manual_overrides (
    tx_key TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);

-- Catégories du seed supprimées ou renommées par l'utilisateur : le seed ne les recrée pas
-- (parent_name '' pour une racine).
CREATE TABLE seed_dismissed (
    name TEXT NOT NULL,
    parent_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(name, parent_name)
);

-- Sources externes (v6) : connexion bancaire Enable Banking, comptes distants, synchros.
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
-- fin v6

CREATE INDEX idx_transactions_op_date ON transactions(op_date);
CREATE INDEX idx_transactions_category_id ON transactions(category_id);
CREATE INDEX idx_transactions_account_id ON transactions(account_id);

CREATE UNIQUE INDEX ux_categories_name_parent ON categories(name, COALESCE(parent_id, 0));
CREATE INDEX idx_transactions_section_id ON transactions(section_id);
CREATE INDEX idx_transactions_mirror_of ON transactions(mirror_of);
CREATE INDEX idx_sections_account_date ON sections(account_id, statement_date);

-- v7 : tickets de caisse
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
-- fin v7

PRAGMA user_version = 7;

COMMIT;
