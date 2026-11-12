"""Couche générique des sources externes : frontière PDF, identité, dédoublonnage, contrôle de solde (données fictives)."""
import json
from datetime import date
from decimal import Decimal

import pytest

from app.sources.base import AccountFetch, FetchedBalance, FetchedTransaction
from app.sources.service import balance_gap, frontier, store_fetch
from app.store import repo
from app.store.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '00000000001', 'Courant fictif', 'checking')")
    c.execute("INSERT INTO documents(id, sha256, stored_path, parser, imported_at) VALUES (1, 's', 's.pdf', 'p', 'x')")
    yield c
    c.close()


def _section(conn, statement_date, closing_cents=10000, status="ok"):
    conn.execute("INSERT INTO sections(document_id, account_id, statement_date, opening_cents, closing_cents, "
                 "gap_cents, status, content_hash) VALUES (1, 1, ?, 0, ?, 0, ?, 'h')",
                 (statement_date, closing_cents, status))


def _tx(day, amount, label="CB MAGASIN FICTIF"):
    return FetchedTransaction(date.fromisoformat(day), None, label, Decimal(amount), {"label": label, "day": day})


def _rows(conn):
    return conn.execute("SELECT op_date, amount_cents, label, source, section_id FROM transactions "
                        "ORDER BY op_date, id").fetchall()


def test_frontier_is_last_ok_statement(conn):
    assert frontier(conn, 1) is None
    _section(conn, "2025-01-10")
    _section(conn, "2025-02-10")
    _section(conn, "2025-03-10", status="error")
    assert frontier(conn, 1) == date(2025, 2, 10)


def test_store_fetch_keeps_only_after_frontier(conn):
    _section(conn, "2025-02-10")
    fetch = AccountFetch(1, [_tx("2025-02-09", "-1.00"), _tx("2025-02-10", "-2.00"), _tx("2025-02-11", "-3.00")])
    ids, skipped = store_fetch(conn, "enablebanking", fetch)
    assert len(ids) == 1 and skipped == 2
    assert _rows(conn) == [("2025-02-11", -300, "CB MAGASIN FICTIF", "enablebanking", None)]


def test_store_fetch_without_pdf_takes_everything(conn):
    ids, skipped = store_fetch(conn, "enablebanking", AccountFetch(1, [_tx("2020-01-01", "5.00")]))
    assert len(ids) == 1 and skipped == 0


def test_resync_overlapping_window_adds_nothing(conn):
    first = [_tx("2025-03-01", "-4.00"), _tx("2025-03-01", "-4.00"), _tx("2025-03-02", "10.00", "VIR FICTIF")]
    assert len(store_fetch(conn, "enablebanking", AccountFetch(1, first))[0]) == 3
    # même fenêtre, ordre différent, plus une opération nouvelle : seule la nouvelle entre
    again = [first[2], _tx("2025-03-03", "-1.00"), first[1], first[0]]
    ids, _ = store_fetch(conn, "enablebanking", AccountFetch(1, again))
    assert len(ids) == 1
    assert [r[0] for r in _rows(conn)] == ["2025-03-01", "2025-03-01", "2025-03-02", "2025-03-03"]


def test_exact_duplicates_on_same_day_are_kept(conn):
    ids, _ = store_fetch(conn, "enablebanking", AccountFetch(1, [_tx("2025-03-01", "-4.00")] * 3))
    assert len(ids) == 3


def test_raw_json_is_stored(conn):
    ids, _ = store_fetch(conn, "enablebanking", AccountFetch(1, [_tx("2025-03-01", "-4.00")]))
    raw = conn.execute("SELECT raw_json FROM external_transactions WHERE transaction_id = ?", (ids[0],)).fetchone()[0]
    assert json.loads(raw) == {"label": "CB MAGASIN FICTIF", "day": "2025-03-01"}


def test_manual_override_is_reapplied(conn):
    cat = conn.execute("INSERT INTO categories(name, kind) VALUES ('Courses', 'expense')").lastrowid
    fetch = AccountFetch(1, [_tx("2025-03-01", "-4.00")])
    tx_id = store_fetch(conn, "enablebanking", fetch)[0][0]
    key = conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    conn.execute("INSERT INTO manual_overrides(tx_key, category_id) VALUES (?, ?)", (key, cat))
    conn.execute("DELETE FROM external_transactions")
    conn.execute("DELETE FROM transactions")
    store_fetch(conn, "enablebanking", fetch)
    assert conn.execute("SELECT category_id, manual, classified_by FROM transactions").fetchone() == (cat, 1, "manual")


def test_balance_gap(conn):
    _section(conn, "2025-02-10", closing_cents=10000)
    store_fetch(conn, "enablebanking", AccountFetch(1, [_tx("2025-02-12", "-25.00"), _tx("2025-02-20", "-5.00")]))
    assert balance_gap(conn, 1, FetchedBalance(Decimal("75.00"), date(2025, 2, 15))) == 0
    assert balance_gap(conn, 1, FetchedBalance(Decimal("70.00"), date(2025, 2, 28))) == 0
    assert balance_gap(conn, 1, FetchedBalance(Decimal("80.40"), date(2025, 2, 28))) == 1040
    assert balance_gap(conn, 1, None) is None
    assert balance_gap(conn, 1, FetchedBalance(Decimal("1"), date(2025, 1, 1))) is None   # solde antérieur au relevé


def test_balance_gap_without_pdf_is_impossible(conn):
    assert balance_gap(conn, 1, FetchedBalance(Decimal("1"), date(2025, 1, 1))) is None


def test_api_overlap(conn):
    _section(conn, "2025-01-10")
    store_fetch(conn, "enablebanking", AccountFetch(1, [_tx("2025-03-05", "-1.00")]))
    assert repo.api_overlap(conn, 1, "2025-02-11", "2025-03-10")      # période 11/02 → 10/03 : contient le 05/03
    assert not repo.api_overlap(conn, 1, "2025-01-11", "2025-02-10")  # trou antérieur : importable
    assert not repo.api_overlap(conn, 1, "2025-03-06", "2025-04-10")  # trou postérieur (synchro interrompue)
    _section(conn, "2025-03-04")
    assert not repo.api_overlap(conn, 1, "2025-02-05", "2025-03-04")  # relevé du 04/03 : opération API après
    assert repo.api_overlap(conn, 1, "2025-02-05", "2025-04-10")      # bornée par le relevé précédent (04/03)


def test_pdf_number_joins_account_created_by_bank(conn):
    bank_account = conn.execute("INSERT INTO accounts(number, label, kind, source) VALUES "
                                "('FR7600000000000000000000399', 'Livret', 'savings', 'enablebanking')").lastrowid
    assert repo.upsert_account(conn, "00000000003", "LIVRET BLEU") == bank_account
    assert repo.upsert_account(conn, "123", "COURT") not in (bank_account, 1)
