"""Rapprochement des tickets avec les débits carte (données fictives insérées en SQL)."""
from decimal import Decimal

import pytest

from app.receipts.matching import candidates, card_cents, link_manual, match_receipts
from app.store.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.executescript("""
        INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'Courant fictif', 'checking'),
                                                             (2, 'A2', 'Livret fictif', 'savings');
    """)
    yield c
    c.close()


def _tx(conn, key, day, cents, label="CB DIVERS", account=1, mirror=None):
    return conn.execute(
        "INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, mirror_of)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", (account, key, day, label, label.lower(), cents, mirror)).lastrowid


def _receipt(conn, n, at, payments, shop="VILLETEST", status="ok"):
    total = sum(a for _, a in payments)
    rid = conn.execute(
        "INSERT INTO receipts(store, sha256, stored_path, origin, shop, purchased_at, total_cents, status,"
        " imported_at) VALUES ('superu', ?, ?, 'upload', ?, ?, ?, ?, '2025-01-01T00:00:00')",
        (f"sha{n}", f"p{n}.pdf", shop, at, total, status)).lastrowid
    for rank, (method, cents) in enumerate(payments):
        conn.execute("INSERT INTO receipt_payments VALUES (?, ?, ?, ?)", (rid, rank, method, cents))
    return rid


def _link(conn, rid):
    return tuple(conn.execute("SELECT tx_key, link_mode, link_broken FROM receipts WHERE id = ?", (rid,)).fetchone())


def test_single_candidate_links_auto(conn):
    _tx(conn, "k1", "2025-01-11", -2828)
    rid = _receipt(conn, 1, "2025-01-10T18:30:00", [("card", 2828)])
    assert match_receipts(conn) == {"linked": 1, "unlinked": 0, "broken": 0}
    assert _link(conn, rid) == ("k1", "auto", 0)


def test_window_account_and_mirror_filters(conn):
    _tx(conn, "late", "2025-01-14", -1000)                       # J+4 : hors fenêtre
    _tx(conn, "early", "2025-01-09", -1000)                      # veille : hors fenêtre
    _tx(conn, "sav", "2025-01-10", -1000, account=2)             # livret
    src = _tx(conn, "src", "2025-01-10", -1000)
    _tx(conn, "mir", "2025-01-10", -1000, mirror=src)            # jambe miroir
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    match_receipts(conn)
    assert _link(conn, rid)[0] == "src"                          # seul "src" est éligible


def test_two_same_amount_receipts_same_day_wait(conn):
    _tx(conn, "k1", "2025-01-10", -1200)
    _tx(conn, "k2", "2025-01-10", -1200)
    a = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1200)])
    b = _receipt(conn, 2, "2025-01-10T11:00:00", [("card", 1200)])
    assert match_receipts(conn)["linked"] == 0
    assert _link(conn, a)[0] is None and _link(conn, b)[0] is None


def test_label_tiebreak_picks_store_debit(conn):
    _tx(conn, "pharma", "2025-01-10", -1500, "CB PHARMACIE")
    _tx(conn, "store", "2025-01-10", -1500, "CB HYPER U VILLETEST")
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1500)], shop="HYPER U")
    match_receipts(conn)
    assert _link(conn, rid)[0] == "store"


def test_tiebreak_ambiguous_after_filter_does_nothing(conn):
    _tx(conn, "a", "2025-01-10", -1500, "CB U NORD")
    _tx(conn, "b", "2025-01-10", -1500, "CB U SUD")
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1500)])
    match_receipts(conn)
    assert _link(conn, rid)[0] is None


def test_meal_voucher_only_receipt_has_no_debit(conn):
    _tx(conn, "k1", "2025-01-10", -1000)
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("meal_voucher", 1000)])
    assert card_cents(conn, rid) == 0
    assert match_receipts(conn)["linked"] == 0
    assert _link(conn, rid)[0] is None
    assert conn.execute("SELECT status FROM receipts WHERE id = ?", (rid,)).fetchone()[0] == "ok"


def test_partial_meal_voucher_matches_card_part(conn):
    _tx(conn, "k1", "2025-01-10", -3000)
    _tx(conn, "k2", "2025-01-10", -1800)
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1800), ("meal_voucher", 1200)])
    match_receipts(conn)
    assert _link(conn, rid)[0] == "k2"


def test_manual_link_is_kept_and_blocks_auto(conn):
    _tx(conn, "k1", "2025-01-10", -1000)
    _tx(conn, "k2", "2025-01-10", -1000, "CB AUTRE")
    manual = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    link_manual(conn, manual, "k1")
    other = _receipt(conn, 2, "2025-01-10T11:00:00", [("card", 1000)])
    match_receipts(conn)
    assert _link(conn, manual) == ("k1", "manual", 0)            # jamais touché
    assert _link(conn, other) == ("k2", "auto", 0)               # k1 est pris, k2 reste seul


def test_vanished_debit_auto_vs_manual(conn):
    _tx(conn, "k1", "2025-01-10", -1000)
    _tx(conn, "k2", "2025-01-10", -2000)
    auto = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    manual = _receipt(conn, 2, "2025-01-10T10:00:00", [("card", 2000)])
    match_receipts(conn)
    link_manual(conn, manual, "k2")
    conn.execute("DELETE FROM transactions")
    _tx(conn, "k3", "2025-01-11", -1000)                         # autre candidat unique pour le lien auto
    _tx(conn, "k4", "2025-01-11", -2000)                         # jamais pris par le lien manuel cassé
    assert match_receipts(conn) == {"linked": 1, "unlinked": 1, "broken": 1}
    assert _link(conn, auto) == ("k3", "auto", 0)
    assert _link(conn, manual) == ("k2", "manual", 1)
    match_receipts(conn)
    assert _link(conn, manual) == ("k2", "manual", 1)            # jamais refait automatiquement
    _tx(conn, "k2", "2025-01-10", -2000)                         # le débit revient
    match_receipts(conn)
    assert _link(conn, manual) == ("k2", "manual", 0)


def test_auto_link_dropped_when_amount_changes(conn):
    _tx(conn, "k1", "2025-01-10", -1000)
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    match_receipts(conn)
    conn.execute("UPDATE receipt_payments SET amount_cents = 900 WHERE receipt_id = ?", (rid,))
    assert match_receipts(conn)["unlinked"] == 1
    assert _link(conn, rid)[0] is None


def test_link_manual_rejects_already_linked_debit(conn):
    _tx(conn, "k1", "2025-01-10", -1000)
    a = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    b = _receipt(conn, 2, "2025-01-10T10:00:00", [("card", 500)])
    link_manual(conn, a, "k1")
    with pytest.raises(ValueError):
        link_manual(conn, b, "k1")
    link_manual(conn, a, "k1")                                   # se relier au même débit reste permis
    link_manual(conn, a, None)
    assert _link(conn, a) == (None, None, 0)


def test_candidates_same_amount_first_then_by_gap(conn):
    _tx(conn, "far", "2025-01-16", -1000)
    _tx(conn, "near", "2025-01-11", -300, "CB PROCHE")
    _tx(conn, "out", "2025-01-30", -1000)
    _tx(conn, "cred", "2025-01-10", 500)
    rid = _receipt(conn, 1, "2025-01-10T10:00:00", [("card", 1000)])
    got = candidates(conn, rid)
    assert [c["tx_key"] for c in got] == ["far", "near"]
    assert got[0] == {"tx_key": "far", "op_date": "2025-01-16", "label": "CB DIVERS", "amount": Decimal("-10.00")}
    assert len(candidates(conn, rid, limit=1)) == 1
