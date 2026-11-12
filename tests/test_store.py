import re
import sqlite3
from decimal import Decimal

import pytest

from app.importer.amounts import from_cents, to_cents
from app.store.db import connect
from app.store.repo import account_kind_from_label, upsert_account


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def test_connect_twice_keeps_user_version(tmp_path):
    path = tmp_path / "t.db"
    c1 = connect(path)
    assert c1.execute("PRAGMA user_version").fetchone()[0] == 6
    c1.close()
    c2 = connect(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c2.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c2.close()


def test_upsert_account_is_idempotent(conn):
    a = upsert_account(conn, "00000000001", "COMPTE TEST")
    b = upsert_account(conn, "00000000001", "COMPTE TEST")
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


@pytest.mark.parametrize(
    "label,kind",
    [
        ("LIVRET BLEU", "savings"),
        ("COMPTE EPARGNE", "savings"),
        ("Épargne logement", "savings"),
        ("COMPTE COURANT", "checking"),
    ],
)
def test_account_kind_from_label(label, kind):
    assert account_kind_from_label(label) == kind


def test_upsert_account_sets_kind(conn):
    acc = upsert_account(conn, "00000000002", "LIVRET BLEU")
    assert conn.execute("SELECT kind FROM accounts WHERE id=?", (acc,)).fetchone()[0] == "savings"


def test_amount_roundtrip_exact(conn):
    acc = upsert_account(conn, "00000000001", "COMPTE TEST")
    conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents)"
        " VALUES (?, 'k1', '2025-01-05', 'OP', 'op', ?)",
        (acc, to_cents(Decimal("-1234.56"))),
    )
    cents = conn.execute("SELECT amount_cents FROM transactions WHERE tx_key='k1'").fetchone()[0]
    assert cents == -123456
    assert from_cents(cents) == Decimal("-1234.56")


def test_upsert_account_does_not_commit(tmp_path):
    path = tmp_path / "t.db"
    c = connect(path)
    upsert_account(c, "00000000009", "COMPTE TEST")
    c.rollback()
    assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    c.close()


def test_future_schema_version_rejected(tmp_path):
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(RuntimeError):
        connect(path)


def test_bad_account_kind_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts(number, label, kind) VALUES ('1', 'X', 'bogus')")


def test_orphan_foreign_key_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents)"
            " VALUES (999, 'k', '2025-01-05', 'OP', 'op', 100)"
        )


def test_duplicate_root_category_rejected(conn):
    conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES ('Courses', NULL, 'expense')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES ('Courses', NULL, 'expense')")


def test_connect_waits_on_busy_database(tmp_path):
    c = connect(tmp_path / "t.db")
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    c.close()


def _as_v5(schema: str) -> str:
    """Schéma courant ramené à la version 5 (sans les sources externes)."""
    v5 = re.sub(r"-- Sources externes \(v6\).*?-- fin v6\n\n", "", schema, flags=re.S)
    v5 = v5.replace("suggestion_support INTEGER NULL,\n    source TEXT NOT NULL DEFAULT 'pdf'\n",
                    "suggestion_support INTEGER NULL\n").replace("user_version = 6", "user_version = 5")
    assert "bank_links" not in v5 and v5.count("source TEXT") == 1 and "user_version = 5" in v5
    return v5


def _as_v4(schema: str) -> str:
    """Schéma courant ramené à la version 4 (sans la table seed_dismissed)."""
    schema = _as_v5(schema)
    v4 = re.sub(r"-- Catégories du seed.*?\);\n\n", "", schema, flags=re.S).replace("user_version = 5", "user_version = 4")
    assert "seed_dismissed" not in v4 and "user_version = 4" in v4
    return v4


def _strip_v3_columns(schema: str) -> str:
    schema = _as_v4(schema)
    stripped = re.sub(r",\n    classified_by .*?suggestion_support INTEGER NULL\n", "\n", schema, flags=re.S)
    assert "classified_by" not in stripped
    return stripped


def _v1_schema() -> str:
    from importlib import resources
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    v1 = _strip_v3_columns(schema).replace(",\n    message TEXT\n", "\n").replace("user_version = 4", "user_version = 1")
    assert "message" not in v1 and "user_version = 1" in v1
    return v1


def test_new_database_has_section_message(tmp_path):
    c = connect(tmp_path / "t.db")
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert "message" in [r[1] for r in c.execute("PRAGMA table_info(sections)")]
    c.close()


def test_migration_from_v1_adds_section_message(tmp_path):
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(_v1_schema())
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    raw.execute("INSERT INTO documents(id, sha256, stored_path, parser, imported_at) "
                "VALUES (1, 's', 's.pdf', 'p', '2025-01-01')")
    raw.execute("INSERT INTO sections(document_id, account_id, statement_date, gap_cents, status, content_hash) "
                "VALUES (1, 1, '2025-01-31', 0, 'ok', 'h')")
    raw.commit()
    raw.close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT status, message FROM sections").fetchall() == [("ok", None)]
    c.close()
    c = connect(path)       # deuxième ouverture : rien à migrer
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    c.close()


def test_move_manual_overrides_never_overwrites(tmp_path):
    from app.store import repo
    c = connect(tmp_path / "m.db")
    cats = [c.execute("INSERT INTO categories(name, kind) VALUES (?, 'expense')", (n,)).lastrowid for n in "abcd"]
    for key, cat in zip(["k1", "k2", "k3", "k4"], cats):
        c.execute("INSERT INTO manual_overrides(tx_key, category_id) VALUES (?, ?)", (key, cat))
    # k1 → k4 (occupé), k2 → k1 (k1 reste en place), k3 → n (libre)
    assert repo.move_manual_overrides(c, {"k1": "k4", "k2": "k1", "k3": "n"}) == 2
    assert dict(c.execute("SELECT tx_key, category_id FROM manual_overrides")) == {
        "k1": cats[0], "k2": cats[1], "n": cats[2], "k4": cats[3]}
    c.close()


def test_migration_2_to_3(tmp_path):
    from importlib import resources
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    v2 = _strip_v3_columns(schema).replace("user_version = 4", "user_version = 2")
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(v2)
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    raw.execute("INSERT INTO categories(id, name, kind) VALUES (1, 'Divers', 'expense')")
    raw.execute("INSERT INTO rules(id, pattern, match, category_id, priority) VALUES (1, 'x', 'contains', 1, 1)")
    cols = "(id, account_id, tx_key, op_date, label, label_norm, amount_cents, category_id, rule_id, manual, mirror_of)"
    rows = [(1, 'k1', -100, 1, None, 1, None), (2, 'k2', -100, 1, 1, 0, None),
            (3, 'k3', 100, 1, None, 0, 1), (4, 'k4', 100, None, None, 0, None),
            (5, 'k5', 100, None, None, 0, 4)]
    for i, key, cents, cat, rule, manual, mirror in rows:
        raw.execute(f"INSERT INTO transactions{cols} VALUES (?, 1, ?, '2024-01-01', 'a', 'a', ?, ?, ?, ?, ?)",
                    (i, key, cents, cat, rule, manual, mirror))
    raw.commit()
    raw.close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT id, classified_by, confidence, suggested_category_id, suggestion_reason "
                     "FROM transactions ORDER BY id").fetchall() == [
        (1, "manual", None, None, None), (2, "rule", None, None, None),
        (3, "pair", None, None, None), (4, None, None, None, None), (5, None, None, None, None)]
    c.close()
    c = connect(path)       # deuxième ouverture : rien à migrer
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT classified_by FROM transactions ORDER BY id").fetchall() == [
        ("manual",), ("rule",), ("pair",), (None,), (None,)]
    c.close()


def test_classified_by_check_constraint(conn):
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, "
                     "classified_by) VALUES (1, 'k', '2024-01-01', 'a', 'a', 1, 'bogus')")


def test_insert_transaction_reapplies_override_with_classified_by(conn):
    from app.store.repo import insert_transaction
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    conn.execute("INSERT INTO categories(id, name, kind) VALUES (1, 'Divers', 'expense')")
    conn.execute("INSERT INTO manual_overrides(tx_key, category_id) VALUES ('kM', 1)")
    a = insert_transaction(conn, None, 1, "kM", "2024-01-01", "2024-01-01", "a", "a", -100)
    b = insert_transaction(conn, None, 1, "kN", "2024-01-01", "2024-01-01", "b", "b", -100)
    assert conn.execute("SELECT manual, category_id, classified_by FROM transactions WHERE id = ?",
                        (a,)).fetchone() == (1, 1, "manual")
    assert conn.execute("SELECT manual, category_id, classified_by FROM transactions WHERE id = ?",
                        (b,)).fetchone() == (0, None, None)


def test_failed_migration_rolls_back_and_closes(tmp_path, monkeypatch):
    from importlib import resources
    import app.store.db as db
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(_strip_v3_columns(schema).replace("user_version = 4", "user_version = 2"))
    raw.close()
    monkeypatch.setattr(db, "_MIGRATE_2_TO_3", "BEGIN; ALTER TABLE transactions ADD COLUMN classified_by TEXT; "
                                              "ALTER TABLE nope ADD COLUMN x TEXT; COMMIT;")
    with pytest.raises(sqlite3.Error):
        connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "classified_by" not in [r[1] for r in raw.execute("PRAGMA table_info(transactions)")]
    raw.close()


def test_migration_3_to_4_adds_suggestion_support(tmp_path):
    from importlib import resources
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    v3 = _as_v4(schema).replace(",\n    suggestion_support INTEGER NULL\n", "\n").replace("user_version = 4", "user_version = 3")
    assert "suggestion_support" not in v3 and "user_version = 3" in v3
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(v3)
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    raw.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, "
                "confidence, suggestion_reason) VALUES (1, 'k', '2024-01-01', 'a', 'a', -1, 0.5, 'r')")
    raw.commit()
    raw.close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT confidence, suggestion_reason, suggestion_support FROM transactions").fetchone() == (
        0.5, "r", None)
    c.close()


def test_migration_4_to_5_adds_seed_dismissed(tmp_path):
    from importlib import resources
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(_as_v4(schema))
    raw.close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT COUNT(*) FROM seed_dismissed").fetchone()[0] == 0
    c.close()


def test_new_database_has_external_sources(tmp_path):
    c = connect(tmp_path / "t.db")
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"bank_links", "bank_auth_states", "external_accounts", "external_transactions", "sync_runs"} <= tables
    assert "source" in [r[1] for r in c.execute("PRAGMA table_info(transactions)")]
    c.close()


def test_migration_from_v5_marks_existing_transactions_pdf(tmp_path):
    from importlib import resources
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(_as_v5(resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")))
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    raw.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
                "VALUES (1, 'k', '2025-01-05', 'OP', 'op', 100)")
    raw.commit()
    raw.close()
    c = connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT source FROM transactions").fetchall() == [("pdf",)]
    assert c.execute("SELECT COUNT(*) FROM bank_links").fetchone()[0] == 0
    c.close()
