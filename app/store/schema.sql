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

-- Portefeuilles Solana (v7)
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
-- fin v7

CREATE INDEX idx_transactions_op_date ON transactions(op_date);
CREATE INDEX idx_transactions_category_id ON transactions(category_id);
CREATE INDEX idx_transactions_account_id ON transactions(account_id);

CREATE UNIQUE INDEX ux_categories_name_parent ON categories(name, COALESCE(parent_id, 0));
CREATE INDEX idx_transactions_section_id ON transactions(section_id);
CREATE INDEX idx_transactions_mirror_of ON transactions(mirror_of);
CREATE INDEX idx_sections_account_date ON sections(account_id, statement_date);

PRAGMA user_version = 7;

COMMIT;
