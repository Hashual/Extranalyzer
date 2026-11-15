from datetime import date, timedelta

import pytest

from app.analytics import holdings as h
from app.analytics.periods import Period
from app.classify.savings_target import savings_category_for_account
from app.store.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


_n = [0]


def _account(conn, label="LIVRET A", kind="savings"):
    _n[0] += 1
    return conn.execute("INSERT INTO accounts(number, label, kind) VALUES (?, ?, ?)",
                        (f"FR{_n[0]:05d}", label, kind)).lastrowid


def _statement(conn, account_id, day, closing, status="ok"):
    _n[0] += 1
    doc = conn.execute("INSERT INTO documents(sha256, filename, stored_path, parser, statement_date, imported_at) "
                       "VALUES (?, 'f', 'p', 'x', ?, '2026-01-01')", (f"sha{_n[0]}", day)).lastrowid
    conn.execute("INSERT INTO sections(document_id, account_id, statement_date, closing_cents, gap_cents, status,"
                 " content_hash) VALUES (?, ?, ?, ?, 0, ?, 'h')", (doc, account_id, day, closing, status))


def _tx(conn, account_id, day, cents, source="pdf", mirror_of=None):
    _n[0] += 1
    return conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents,"
                        " source, mirror_of) VALUES (?, ?, ?, 'l', 'l', ?, ?, ?)",
                        (account_id, f"k{_n[0]}", day, cents, source, mirror_of)).lastrowid


def _wallet(conn, label="W", category_id=None):
    _n[0] += 1
    return conn.execute("INSERT INTO wallets(address, label, savings_category_id, created_at) "
                        "VALUES (?, ?, ?, '2026-01-01')", (f"addr{_n[0]}", label, category_id)).lastrowid


def _snap(conn, wallet_id, taken_at, cents, status="ok", unpriced=0):
    return conn.execute("INSERT INTO wallet_snapshots(wallet_id, taken_at, status, total_eur_cents, unpriced_count)"
                        " VALUES (?, ?, ?, ?, ?)", (wallet_id, taken_at, status, cents, unpriced)).lastrowid


def _pos(conn, snap, kind, cents, mint=None, symbol=None):
    conn.execute("INSERT INTO wallet_positions(snapshot_id, kind, mint, symbol, amount, value_eur_cents) "
                 "VALUES (?, ?, ?, ?, '1', ?)", (snap, kind, mint, symbol, cents))


def test_savings_balance_from_statement_and_later_ops(conn):
    a = _account(conn)
    _statement(conn, a, "2026-01-31", 100_000)
    _tx(conn, a, "2026-02-03", 5_000, source="enablebanking")
    _tx(conn, a, "2026-02-10", -2_000)
    assert h.savings_balance(conn, a, date(2026, 2, 15)) == 103_000
    assert h.savings_balance(conn, a, date(2026, 2, 5)) == 105_000
    assert h.savings_balance(conn, a, date(2026, 1, 31)) == 100_000
    assert h.savings_balance(conn, a, date(2026, 1, 15)) is None


def test_wallet_value_carries_last_ok_snapshot(conn):
    w = _wallet(conn)
    _snap(conn, w, "2026-11-10T08:00:00+00:00", 1_000)
    _snap(conn, w, "2026-11-12T08:00:00+00:00", 1_500)
    _snap(conn, w, "2026-11-13T08:00:00+00:00", None, status="error")
    assert h.wallet_value(conn, w, date(2026, 11, 14)) == 1_500
    assert h.wallet_value(conn, w, date(2026, 11, 11)) == 1_000
    assert h.wallet_value(conn, w, date(2026, 11, 9)) is None


def test_series_daily_and_weekly(conn):
    a = _account(conn)
    _statement(conn, a, "2020-01-01", 1_000)
    w = _wallet(conn)
    _snap(conn, w, "2026-06-05T08:00:00+00:00", 500)
    short = h.series(conn, date(2026, 6, 1), date(2026, 6, 10))
    assert len(short) == 10
    assert short[0]["crypto"] is None and short[3]["crypto"] is None
    assert short[4]["crypto"] == 500 and short[4]["savings"] == 1_000
    end = date(2026, 6, 10)
    long = h.series(conn, end - timedelta(days=499), end)
    assert 70 <= len(long) <= 74
    assert long[-1]["date"] == end
    assert long[-1]["crypto"] == 500


def test_dashboard_change_is_per_place(conn):
    a = _account(conn)
    _statement(conn, a, "2026-01-31", 100_000)
    _tx(conn, a, "2026-02-10", 5_000)
    w = _wallet(conn)
    _snap(conn, w, "2026-02-12T08:00:00+00:00", 7_000)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 20))
    assert d["total"]["change"] == 5_000
    assert d["total"]["change_scope"] == "partial"
    assert d["total"]["value"] == 112_000
    assert d["as_of"] == date(2026, 2, 28)


def test_dashboard_unread_wallet_not_zero(conn):
    _wallet(conn)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 20))
    place = d["places"][0]
    assert place["value"] is None and place["unknown"] is True
    assert d["crypto"]["unread_count"] == 1
    assert d["crypto"]["value"] is None and d["total"]["value"] is None
    assert d["total"]["change_scope"] is None


def test_dashboard_past_period_before_crypto(conn):
    w = _wallet(conn)
    s = _snap(conn, w, "2026-06-01T08:00:00+00:00", 9_000)
    _pos(conn, s, "sol", 9_000)
    d = h.dashboard(conn, Period(date(2026, 1, 1), date(2026, 1, 31)), date(2026, 11, 15))
    assert d["crypto"]["value"] is None
    assert d["allocation"]["by_asset"] == []


def test_dashboard_future_end(conn):
    a = _account(conn)
    _statement(conn, a, "2026-01-31", 100_000)
    today = date(2026, 3, 10)
    d = h.dashboard(conn, Period(date(2026, 3, 1), date(2026, 3, 31)), today)
    assert d["as_of"] == today
    assert d["series"][-1]["date"] == today


def test_by_asset_groups_and_hides(conn):
    w = _wallet(conn)
    s = _snap(conn, w, "2026-02-01T08:00:00+00:00", 0)
    for i in range(9):
        _pos(conn, s, "token", 1_000 + i * 100, mint=f"m{i}", symbol=f"T{i}")
    _pos(conn, s, "token", 50_000, mint="hid", symbol="HID")
    conn.execute("INSERT INTO hidden_mints(mint) VALUES ('hid')")
    _pos(conn, s, "token", None, mint="nov", symbol="NOV")
    _pos(conn, s, "stake", 20_000)
    _pos(conn, s, "sol", 30_000)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))
    parts = d["allocation"]["by_asset"]
    names = [p["name"] for p in parts]
    assert len(parts) == h.ASSET_SLICES + 1 and names[-1] == "Autres"
    assert "HID" not in names and "NOV" not in names
    assert names[:2] == ["SOL disponible", "SOL en staking"]
    assert [p["value"] for p in parts] == sorted((p["value"] for p in parts[:-1]), reverse=True) + [parts[-1]["value"]]


def test_category_lookup_does_not_create(conn):
    a = _account(conn, label="LDDS QUELCONQUE")
    before = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    assert savings_category_for_account(conn, a, create=False) is None
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before


def test_wallet_without_category_has_unknown_deposit(conn):
    cat = savings_category_for_account(conn, _account(conn, label="LIVRET A"))
    w1 = _wallet(conn, "sans catégorie")
    _snap(conn, w1, "2026-02-01T08:00:00+00:00", 7_000)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))
    place = next(p for p in d["places"] if p["kind"] == "wallet")
    assert place["deposited"] is None and place["gap"] is None
    assert d["crypto"]["deposited"] is None and d["crypto"]["gap"] is None
    w2 = _wallet(conn, "avec catégorie", category_id=cat)
    _snap(conn, w2, "2026-02-01T08:00:00+00:00", 3_000)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))
    assert d["crypto"]["value"] == 10_000
    assert d["crypto"]["deposited"] == 0 and d["crypto"]["gap"] == 3_000


def test_savings_balance_uses_latest_ok_statement_and_ignores_error(conn):
    a = _account(conn)
    _statement(conn, a, "2026-01-31", 100_000)
    _statement(conn, a, "2026-02-28", 120_000)
    _statement(conn, a, "2026-03-31", 999_999, status="error")
    _tx(conn, a, "2026-02-10", 5_000)          # déjà dans la clôture du 02-28
    assert h.savings_balance(conn, a, date(2026, 3, 15)) == 120_000
    assert h.savings_balance(conn, a, date(2026, 2, 15)) == 105_000


def test_savings_balance_counts_mirror_leg(conn):
    a = _account(conn)
    other = _account(conn, label="COURANT", kind="checking")
    _statement(conn, a, "2026-01-31", 10_000)
    first = _tx(conn, other, "2026-02-02", -4_000)
    _tx(conn, a, "2026-02-02", 4_000, mirror_of=first)
    assert h.savings_balance(conn, a, date(2026, 2, 5)) == 14_000


def test_dashboard_change_scope_all(conn):
    a = _account(conn)
    _statement(conn, a, "2026-01-31", 100_000)
    _tx(conn, a, "2026-02-10", 5_000)
    w = _wallet(conn)
    _snap(conn, w, "2026-01-20T08:00:00+00:00", 6_000)
    _snap(conn, w, "2026-02-12T08:00:00+00:00", 7_000)
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 20))
    assert d["total"]["change"] == 6_000 and d["total"]["change_scope"] == "all"
