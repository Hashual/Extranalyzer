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
    assert c1.execute("PRAGMA user_version").fetchone()[0] == 2
    c1.close()
    c2 = connect(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == 2
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


def _v1_schema() -> str:
    from importlib import resources
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    v1 = schema.replace(",\n    message TEXT\n", "\n").replace("user_version = 2", "user_version = 1")
    assert "message" not in v1 and "user_version = 1" in v1
    return v1


def test_new_database_has_section_message(tmp_path):
    c = connect(tmp_path / "t.db")
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
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
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert c.execute("SELECT status, message FROM sections").fetchall() == [("ok", None)]
    c.close()
    c = connect(path)       # deuxième ouverture : rien à migrer
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    c.close()
