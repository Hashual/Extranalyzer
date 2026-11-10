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
    content_hash TEXT NOT NULL
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
    account_id INTEGER NULL,
    category_id INTEGER NOT NULL,
    priority INTEGER NOT NULL
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    section_id INTEGER REFERENCES sections(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    tx_key TEXT UNIQUE NOT NULL,
    op_date TEXT NOT NULL,
    value_date TEXT,
    label TEXT NOT NULL,
    label_norm TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    category_id INTEGER NULL,
    rule_id INTEGER NULL,
    manual INTEGER NOT NULL DEFAULT 0,
    mirror_of INTEGER NULL REFERENCES transactions(id)
);

CREATE TABLE manual_overrides (
    tx_key TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL
);

CREATE INDEX idx_transactions_op_date ON transactions(op_date);
CREATE INDEX idx_transactions_category_id ON transactions(category_id);
CREATE INDEX idx_transactions_account_id ON transactions(account_id);

PRAGMA user_version = 1;
