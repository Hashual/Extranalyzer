from decimal import Decimal

import pytest

from app.classify.savings_target import savings_category_named
from app.sources.solana import wallets as w
from app.sources.solana.holdings import Position, WalletReading
from app.store.db import connect

ADDR = "11111111111111111111111111111112"          # 32 octets : 31 zéros puis 1
ADDR2 = "11111111111111111111111111111113"
MINT = "MintFictif1111111111111111111111111111111"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def _reading(token_cents=5000):
    pos = [Position(kind="sol", amount=Decimal("1.5"), mint="So1", symbol="SOL", price_eur=Decimal("100"),
                    price_source="helius", value_eur_cents=15000),
           Position(kind="token", amount=Decimal("10"), mint=MINT, symbol="TKN", decimals=6,
                    price_eur=Decimal("5"), price_source="helius", value_eur_cents=token_cents),
           Position(kind="stake", amount=Decimal("2"), stake_account="Stk", vote_account="Vote",
                    stake_state="active", value_eur_cents=None)]
    return WalletReading(positions=pos, total_eur_cents=15000 + token_cents, unpriced_count=1)


def _cat_name(conn, cat_id):
    return conn.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)).fetchone()[0]


def test_clean_address():
    assert w.clean_address(f"  {ADDR} \n") == ADDR
    for bad in ("0x52908400098527886E0F7030069857D2E4169EE7", "1" * 20, "0OIl" + "1" * 28, ""):
        with pytest.raises(w.WalletError, match="adresse Solana invalide"):
            w.clean_address(bad)


def test_add_creates_savings_category(conn):
    a = w.add_wallet(conn, "Principal", ADDR, "2026-01-01")
    b = w.add_wallet(conn, "principal", ADDR2, "2026-01-01")
    cats = [conn.execute("SELECT savings_category_id FROM wallets WHERE id = ?", (i,)).fetchone()[0] for i in (a, b)]
    assert cats[0] == cats[1] is not None
    parent = conn.execute("SELECT p.name FROM categories c JOIN categories p ON p.id = c.parent_id WHERE c.id = ?",
                          (cats[0],)).fetchone()[0]
    assert parent == "Épargne" and _cat_name(conn, cats[0]) == "Principal"
    with pytest.raises(w.WalletError):
        w.add_wallet(conn, "   ", "x", "2026-01-01")
    with pytest.raises(w.WalletError):
        w.add_wallet(conn, "x" * 61, ADDR, "2026-01-01")


def test_add_duplicate(conn):
    w.add_wallet(conn, "A", ADDR, "2026-01-01")
    with pytest.raises(w.DuplicateWallet, match="adresse déjà suivie"):
        w.add_wallet(conn, "B", f" {ADDR} ", "2026-01-01")


def test_rename_renames_category_when_unchanged(conn):
    wid = w.add_wallet(conn, "Principal", ADDR, "t")
    cat = conn.execute("SELECT savings_category_id FROM wallets").fetchone()[0]
    w.rename_wallet(conn, wid, "Ledger")
    assert _cat_name(conn, cat) == "Ledger"
    conn.execute("UPDATE categories SET name = 'Perso' WHERE id = ?", (cat,))
    w.rename_wallet(conn, wid, "Autre")
    assert _cat_name(conn, cat) == "Perso"
    assert conn.execute("SELECT label FROM wallets").fetchone()[0] == "Autre"


def test_rename_with_sibling_conflict(conn):
    w.add_wallet(conn, "Un", ADDR, "t")
    wid = w.add_wallet(conn, "Deux", ADDR2, "t")
    w.rename_wallet(conn, wid, "un")
    assert conn.execute("SELECT label FROM wallets WHERE id = ?", (wid,)).fetchone()[0] == "un"
    assert _cat_name(conn, conn.execute("SELECT savings_category_id FROM wallets WHERE id = ?",
                                        (wid,)).fetchone()[0]) == "Deux"


def _tx(conn, key, account, cents, cat, mirror_of=None):
    return conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents,"
                        " category_id, mirror_of) VALUES (?,?,?,?,?,?,?,?)",
                        (account, key, "2026-01-02", "L", "L", cents, cat, mirror_of)).lastrowid


def _accounts(conn):
    return [conn.execute("INSERT INTO accounts(number, label, kind) VALUES (?,?,?)", (n, n, k)).lastrowid
            for n, k in (("1", "checking"), ("2", "savings"))]


def test_remove_keeps_category_and_transactions(conn):
    wid = w.add_wallet(conn, "Principal", ADDR, "t")
    cat = conn.execute("SELECT savings_category_id FROM wallets").fetchone()[0]
    acc = _accounts(conn)[0]
    _tx(conn, "k1", acc, -10000, cat)
    w.record_reading(conn, wid, "2026-01-02", _reading())
    w.remove_wallet(conn, wid)
    assert conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM wallet_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM wallet_positions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE id = ?", (cat,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_net_deposited(conn):
    w.add_wallet(conn, "Principal", ADDR, "t")
    cat = conn.execute("SELECT savings_category_id FROM wallets").fetchone()[0]
    chk, liv = _accounts(conn)
    first = _tx(conn, "k1", chk, -10000, cat)
    _tx(conn, "k2", liv, -5000, cat)
    _tx(conn, "k3", chk, 3000, cat)
    _tx(conn, "k4", liv, 10000, cat, mirror_of=first)
    wal = w.overview(conn)["wallets"][0]
    assert (wal["deposits"], wal["withdrawals"], wal["net_deposited"]) == (
        Decimal("150"), Decimal("30"), Decimal("120"))


def test_net_deposited_without_category(conn):
    wid = w.add_wallet(conn, "Principal", ADDR, "t")
    conn.execute("UPDATE wallets SET savings_category_id = NULL WHERE id = ?", (wid,))
    wal = w.overview(conn)["wallets"][0]
    assert wal["net_deposited"] == wal["deposits"] == wal["withdrawals"] == 0
    assert wal["category_name"] is None


def test_error_keeps_last_ok(conn):
    wid = w.add_wallet(conn, "Principal", ADDR, "t")
    w.record_reading(conn, wid, "2026-01-02T10:00", _reading())
    w.record_error(conn, wid, "2026-01-02T11:00", "x" * 500)
    wal = w.overview(conn)["wallets"][0]
    assert len(wal["last_error"]) == 200
    assert wal["total_eur"] == Decimal("200.00")
    assert len(wal["positions"]) == 3
    assert wal["unpriced_count"] == 1
    assert [h["taken_at"] for h in wal["history"]] == ["2026-01-02T10:00"]
    w.record_reading(conn, wid, "2026-01-02T12:00", _reading())
    assert w.overview(conn)["wallets"][0]["last_error"] is None


def test_overview_recomputes_with_hidden_mints(conn):
    wid = w.add_wallet(conn, "Principal", ADDR, "t")
    w.record_reading(conn, wid, "2026-01-02T10:00", _reading())
    w.set_validator_label(conn, "Vote", "Mon validateur")
    assert w.overview(conn)["total_eur"] == Decimal("200.00")
    w.hide_mint(conn, MINT, True)
    assert w.hidden_mints(conn) == frozenset({MINT})
    ov = w.overview(conn)
    wal = ov["wallets"][0]
    assert ov["total_eur"] == wal["total_eur"] == Decimal("150.00")
    assert wal["history"][0]["total_eur"] == Decimal("200.00")
    assert {p["mint"]: p["hidden"] for p in wal["positions"] if p["mint"]}[MINT] is True
    assert [p["validator_label"] for p in wal["positions"] if p["kind"] == "stake"] == ["Mon validateur"]
    assert ov["validators"] == {"Vote": "Mon validateur"} and ov["hidden_mints"] == [MINT]
    assert ov["last_refresh"] == "2026-01-02T10:00"
    w.hide_mint(conn, MINT, False)
    w.set_validator_label(conn, "Vote", "")
    assert w.overview(conn)["total_eur"] == Decimal("200.00") and w.overview(conn)["validators"] == {}


def test_savings_category_named_idempotent(conn):
    assert savings_category_named(conn, "Ledger") == savings_category_named(conn, "LEDGER")
