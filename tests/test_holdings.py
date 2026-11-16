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
    c = d["crypto"]
    assert c["value"] == 10_000
    assert c["deposited"] == 0 and c["compared_value"] == 3_000 and c["compared_partial"] is True
    assert c["gap"] == c["compared_value"] - c["deposited"] == 3_000


def test_unread_wallet_with_known_deposit_is_excluded_from_comparison(conn):
    from app.classify.savings_target import savings_category_named
    cat1, cat2 = savings_category_named(conn, "Crypto 1"), savings_category_named(conn, "Crypto 2")
    other = _account(conn, label="COURANT", kind="checking")
    for cat, cents in ((cat1, -1_000), (cat2, -5_000)):
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (cat, _tx(conn, other, "2026-01-05", cents)))
    w1 = _wallet(conn, "lu", category_id=cat1)
    _snap(conn, w1, "2026-02-01T08:00:00+00:00", 3_000)
    _wallet(conn, "non lu", category_id=cat2)
    c = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))["crypto"]
    assert c["unread_count"] == 1 and c["value"] == 3_000
    assert c["deposited"] == 1_000 and c["compared_value"] == 3_000 and c["gap"] == 2_000
    assert c["compared_partial"] is False


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


def test_dashboard_current_value_excludes_hidden_mints(conn):
    w = _wallet(conn)
    s = _snap(conn, w, "2026-02-01T08:00:00+00:00", 60_000, unpriced=1)
    _pos(conn, s, "sol", 30_000)
    _pos(conn, s, "token", 20_000, mint="hid", symbol="HID")
    _pos(conn, s, "token", None, mint="hid2", symbol="HID2")
    _pos(conn, s, "token", 10_000, mint="ok", symbol="OK")
    period = Period(date(2026, 2, 1), date(2026, 2, 28))
    before = h.dashboard(conn, period, date(2026, 3, 1))
    assert before["crypto"]["value"] == 60_000 and before["crypto"]["unpriced_count"] == 1
    conn.execute("INSERT INTO hidden_mints(mint) VALUES ('hid')")
    conn.execute("INSERT INTO hidden_mints(mint) VALUES ('hid2')")
    d = h.dashboard(conn, period, date(2026, 3, 1))
    assert d["crypto"]["value"] == 40_000 and d["total"]["value"] == 40_000
    assert d["crypto"]["unpriced_count"] == 0
    assert next(p for p in d["places"] if p["kind"] == "wallet")["value"] == 40_000
    assert [p["value"] for p in d["allocation"]["by_place"]] == [40_000]
    assert sum(p["value"] for p in d["allocation"]["by_asset"]) == d["crypto"]["value"]


def test_dashboard_value_matches_overview_when_mint_hidden_before_snapshot(conn):
    from app.sources.solana.wallets import overview
    w = _wallet(conn)
    conn.execute("INSERT INTO hidden_mints(mint) VALUES ('hid')")
    # le relevé a été pris alors que « hid » était déjà masqué : le total stocké l'exclut déjà
    s = _snap(conn, w, "2026-02-01T08:00:00+00:00", 40_000, unpriced=0)
    _pos(conn, s, "sol", 30_000)
    _pos(conn, s, "token", 10_000, mint="ok", symbol="OK")
    _pos(conn, s, "token", 20_000, mint="hid", symbol="HID")
    _pos(conn, s, "token", None, mint="hid", symbol="HID")
    d = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))
    expected = overview(conn)["wallets"][0]
    assert d["crypto"]["value"] == 40_000 == int(expected["total_eur"] * 100)
    assert d["crypto"]["unpriced_count"] == expected["unpriced_count"] == 0


def test_dashboard_unpriced_never_negative(conn):
    w = _wallet(conn)
    conn.execute("INSERT INTO hidden_mints(mint) VALUES ('hid')")
    s = _snap(conn, w, "2026-02-01T08:00:00+00:00", 5_000, unpriced=0)   # stocké hors « hid », sans prix
    _pos(conn, s, "sol", 5_000)
    _pos(conn, s, "token", None, mint="hid", symbol="HID")
    c = h.dashboard(conn, Period(date(2026, 2, 1), date(2026, 2, 28)), date(2026, 3, 1))["crypto"]
    assert c["unpriced_count"] == 0 and c["value"] == 5_000


# --- solde annoncé par la banque (Enable Banking) -------------------------------------------------------------

def _bank_balance(conn, account_id, day, cents, ignored=0, link_status="active"):
    _n[0] += 1
    link = conn.execute("INSERT INTO bank_links(provider, session_id, aspsp_name, aspsp_country, valid_until, status,"
                        " created_at) VALUES ('enablebanking', ?, 'Banque', 'FR', '2099-01-01', ?, '2026-01-01')",
                        (f"s{_n[0]}", link_status)).lastrowid
    conn.execute("INSERT INTO external_accounts(link_id, uid, account_id, ignored, balance_cents, balance_date) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (link, f"u{_n[0]}", account_id, ignored, cents, day))


def test_bank_balance_is_the_current_value_despite_a_history_hole(conn):
    """Un trou d'historique (opération jamais importée) : la valeur actuelle suit la banque."""
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _tx(conn, a, "2026-11-05", 10_000, source="enablebanking")
    _bank_balance(conn, a, "2026-11-10", 130_000)          # 20 000 versés que l'appli n'a jamais vus
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 130_000
    assert h.savings_balance(conn, a, date(2026, 11, 15)) == 130_000


def test_bank_balance_then_later_ops(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _bank_balance(conn, a, "2026-11-10", 130_000)
    _tx(conn, a, "2026-11-12", 1_000, source="enablebanking")
    assert h.savings_balance(conn, a, date(2026, 11, 12)) == 131_000


def test_days_between_last_statement_and_bank_balance_are_anchored_backwards(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _tx(conn, a, "2026-11-05", 10_000, source="enablebanking")
    _bank_balance(conn, a, "2026-11-10", 130_000)
    # remonté depuis le solde de la banque : 130 000 − opérations après le jour demandé
    assert h.savings_balance(conn, a, date(2026, 11, 7)) == 130_000
    assert h.savings_balance(conn, a, date(2026, 11, 4)) == 120_000
    # avant (et au jour du) dernier relevé PDF : le relevé fait foi
    assert h.savings_balance(conn, a, date(2026, 10, 31)) == 100_000


def test_bank_balance_without_any_statement(conn):
    a = _account(conn)
    _tx(conn, a, "2026-11-02", 5_000, source="enablebanking")
    _bank_balance(conn, a, "2026-11-10", 50_000)
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 50_000
    assert h.savings_balance(conn, a, date(2026, 11, 3)) == 50_000
    assert h.savings_balance(conn, a, date(2026, 11, 1)) is None        # avant la première opération connue


def test_latest_bank_balance_wins_and_ignored_rows_do_not_count(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _bank_balance(conn, a, "2026-11-05", 110_000, link_status="replaced")
    _bank_balance(conn, a, "2026-11-10", 120_000)
    _bank_balance(conn, a, "2026-11-12", 999_999, ignored=1)
    assert h.savings_balance(conn, a, date(2026, 11, 12)) == 120_000


def test_bank_balance_older_than_last_statement_is_ignored(conn):
    a = _account(conn)
    _bank_balance(conn, a, "2026-10-15", 1)
    _statement(conn, a, "2026-10-31", 100_000)
    assert h.savings_balance(conn, a, date(2026, 11, 2)) == 100_000


def test_dashboard_uses_bank_balance(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _bank_balance(conn, a, "2026-11-10", 130_000)
    d = h.dashboard(conn, Period(date(2026, 11, 1), date(2026, 11, 30)), date(2026, 11, 15))
    assert d["savings"]["value"] == 130_000
    assert d["places"][0]["value"] == 130_000


def test_bank_balance_alone_without_ops_or_statement(conn):
    a = _account(conn)
    _bank_balance(conn, a, "2026-11-10", 42_000)
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 42_000
    assert h.savings_balance(conn, a, date(2026, 11, 15)) == 42_000
    assert h.savings_balance(conn, a, date(2026, 11, 9)) is None


def test_bank_balance_on_statement_day_is_ignored_and_op_on_statement_day_not_counted(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _tx(conn, a, "2026-10-31", 7_000)                    # déjà dans la clôture du relevé
    _bank_balance(conn, a, "2026-10-31", 1)
    assert h.savings_balance(conn, a, date(2026, 11, 2)) == 100_000


# --- livret non synchronisé : virements déduits du compte courant, solde saisi à la main ----------------------

def _checking_transfer(conn, checking, day, cents, category_id, mirror_of=None):
    t = _tx(conn, checking, day, cents, source="enablebanking", mirror_of=mirror_of)
    conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (category_id, t))
    return t


def _livret_with_category(conn, label="LIVRET BLEU"):
    a = _account(conn, label)
    cat = savings_category_for_account(conn, a)          # « Épargne / Livret Bleu »
    return a, cat


def test_transfers_from_checking_complete_an_unsynced_livret(conn):
    a, cat = _livret_with_category(conn)
    cc = _account(conn, "C/C EUROCOMPTE", "checking")
    _statement(conn, a, "2026-10-31", 100_000)
    _checking_transfer(conn, cc, "2026-11-05", -20_000, cat)     # versement vers le livret
    _checking_transfer(conn, cc, "2026-11-08", 5_000, cat)       # retrait du livret
    _checking_transfer(conn, cc, "2026-10-20", -99_000, cat)     # avant le relevé : déjà dans sa clôture
    led = h._Ledger(conn, a)
    assert led.value(date(2026, 11, 10)) == 115_000
    assert led.value(date(2026, 11, 6)) == 120_000
    assert led.source(date(2026, 11, 10)) == "estimated"
    assert led.source(date(2026, 10, 31)) == "statement"


def test_paired_transfers_are_not_counted_twice(conn):
    a, cat = _livret_with_category(conn)
    cc = _account(conn, "C/C EUROCOMPTE", "checking")
    _statement(conn, a, "2026-10-31", 100_000)
    leg = _tx(conn, a, "2026-11-05", 20_000)                      # jambe côté livret (relevé ultérieur non encore…)
    _checking_transfer(conn, cc, "2026-11-05", -20_000, cat, mirror_of=leg)
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 120_000


def test_synced_livret_does_not_use_inferred_transfers(conn):
    a, cat = _livret_with_category(conn)
    cc = _account(conn, "C/C EUROCOMPTE", "checking")
    _statement(conn, a, "2026-10-31", 100_000)
    _bank_balance(conn, a, "2026-11-10", 130_000)
    _checking_transfer(conn, cc, "2026-11-05", -20_000, cat)
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 130_000


def test_manual_balance_anchors_and_transfers_continue_after_it(conn):
    a, cat = _livret_with_category(conn)
    cc = _account(conn, "C/C EUROCOMPTE", "checking")
    _statement(conn, a, "2026-10-31", 100_000)
    _checking_transfer(conn, cc, "2026-11-05", -20_000, cat)
    h.set_manual_balance(conn, a, date(2026, 11, 10), 125_000, "2026-11-10T09:00:00+00:00")
    _checking_transfer(conn, cc, "2026-11-12", -1_000, cat)
    led = h._Ledger(conn, a)
    assert led.value(date(2026, 11, 10)) == 125_000
    assert led.value(date(2026, 11, 12)) == 126_000
    assert led.value(date(2026, 11, 6)) == 125_000              # remonté depuis la saisie
    assert led.value(date(2026, 11, 4)) == 105_000
    assert led.source(date(2026, 11, 12)) == "manual"


def test_manual_balance_wins_over_older_bank_balance_and_newer_bank_wins_over_it(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    _bank_balance(conn, a, "2026-11-05", 110_000)
    h.set_manual_balance(conn, a, date(2026, 11, 8), 111_111, "x")
    assert h.savings_balance(conn, a, date(2026, 11, 9)) == 111_111
    _bank_balance(conn, a, "2026-11-12", 120_000)
    assert h.savings_balance(conn, a, date(2026, 11, 12)) == 120_000


def test_manual_balance_replaced_and_cleared(conn):
    a = _account(conn)
    _statement(conn, a, "2026-10-31", 100_000)
    h.set_manual_balance(conn, a, date(2026, 11, 8), 1, "x")
    h.set_manual_balance(conn, a, date(2026, 11, 8), 2, "y")
    assert h.savings_balance(conn, a, date(2026, 11, 9)) == 2
    h.clear_manual_balance(conn, a)
    assert h.savings_balance(conn, a, date(2026, 11, 9)) == 100_000


def test_dashboard_place_exposes_balance_source(conn):
    a, cat = _livret_with_category(conn)
    cc = _account(conn, "C/C EUROCOMPTE", "checking")
    _statement(conn, a, "2026-10-31", 100_000)
    _checking_transfer(conn, cc, "2026-11-05", -20_000, cat)
    d = h.dashboard(conn, Period(date(2026, 11, 1), date(2026, 11, 30)), date(2026, 11, 15))
    place = next(p for p in d["places"] if p["kind"] == "savings")
    assert place["value"] == 120_000
    assert place["balance_source"] == "estimated" and place["balance_as_of"] == date(2026, 10, 31)


def test_only_checking_transfers_are_inferred(conn):
    a, cat = _livret_with_category(conn)
    other = _account(conn, "LIVRET JEUNE")                       # un autre livret, pas un compte courant
    _statement(conn, a, "2026-10-31", 100_000)
    _checking_transfer(conn, other, "2026-11-05", -20_000, cat)
    assert h.savings_balance(conn, a, date(2026, 11, 10)) == 100_000
