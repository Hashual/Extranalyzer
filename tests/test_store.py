from decimal import Decimal

import pytest

from app.importer.amounts import from_cents, to_cents
from app.store.db import connect
from app.store.repo import account_kind_from_label, upsert_account


def test_connect_twice_keeps_user_version(tmp_path):
    path = tmp_path / "t.db"
    c1 = connect(path)
    assert c1.execute("PRAGMA user_version").fetchone()[0] == 1
    c1.close()
    c2 = connect(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == 1
    assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c2.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c2.close()


def test_upsert_account_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    a = upsert_account(conn, "00000000001", "COMPTE TEST")
    b = upsert_account(conn, "00000000001", "COMPTE TEST")
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


@pytest.mark.parametrize(
    "label,kind",
    [
        ("LIVRET BLEU", "savings"),
        ("COMPTE EPARGNE", "savings"),
        ("COMPTE COURANT", "checking"),
    ],
)
def test_account_kind_from_label(label, kind):
    assert account_kind_from_label(label) == kind


def test_upsert_account_sets_kind(tmp_path):
    conn = connect(tmp_path / "t.db")
    acc = upsert_account(conn, "00000000002", "LIVRET BLEU")
    assert conn.execute("SELECT kind FROM accounts WHERE id=?", (acc,)).fetchone()[0] == "savings"


def test_amount_roundtrip_exact(tmp_path):
    conn = connect(tmp_path / "t.db")
    acc = upsert_account(conn, "00000000001", "COMPTE TEST")
    conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents)"
        " VALUES (?, 'k1', '2025-01-05', 'OP', 'op', ?)",
        (acc, to_cents(Decimal("-1234.56"))),
    )
    cents = conn.execute("SELECT amount_cents FROM transactions WHERE tx_key='k1'").fetchone()[0]
    assert cents == -123456
    assert from_cents(cents) == Decimal("-1234.56")
