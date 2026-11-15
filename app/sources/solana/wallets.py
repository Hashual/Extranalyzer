"""Portefeuilles Solana en base : suivi, relevés, lien avec l'épargne (net versé) et vue d'ensemble.

Les fonctions n'appellent pas `commit` : l'appelant gère la transaction, comme `app.store.repo`.
"""
import sqlite3
from decimal import Decimal

from app.classify.savings_target import savings_category_named
from app.importer.amounts import from_cents
from app.sources.solana.holdings import WalletReading
from app.store.repo import fold

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
LABEL_MAX = 60
ERROR_MAX = 200


class WalletError(ValueError):
    """Message destiné à l'utilisateur."""


class DuplicateWallet(WalletError):
    pass


def clean_address(raw: str) -> str:
    """Adresse Solana : base58 (alphabet Bitcoin) décodant exactement 32 octets."""
    address = (raw or "").strip()
    value = 0
    for char in address:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise WalletError("adresse Solana invalide")
        value = value * 58 + digit
    zeros = len(address) - len(address.lstrip("1"))
    size = zeros + (value.bit_length() + 7) // 8
    if not address or size != 32:
        raise WalletError("adresse Solana invalide")
    return address


def _clean_label(label: str) -> str:
    label = (label or "").strip()
    if not label:
        raise WalletError("libellé requis")
    if len(label) > LABEL_MAX:
        raise WalletError(f"libellé trop long ({LABEL_MAX} caractères au plus)")
    return label


def add_wallet(conn: sqlite3.Connection, label: str, address: str, now: str) -> int:
    label = _clean_label(label)
    address = clean_address(address)
    if conn.execute("SELECT 1 FROM wallets WHERE address = ?", (address,)).fetchone():
        raise DuplicateWallet("adresse déjà suivie")
    category_id = savings_category_named(conn, label)
    return conn.execute("INSERT INTO wallets(address, label, savings_category_id, created_at) VALUES (?,?,?,?)",
                        (address, label, category_id, now)).lastrowid


def rename_wallet(conn: sqlite3.Connection, wallet_id: int, label: str) -> None:
    label = _clean_label(label)
    row = conn.execute("SELECT label, savings_category_id FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
    if row is None:
        raise WalletError("portefeuille introuvable")
    old_label, cat_id = row
    conn.execute("UPDATE wallets SET label = ? WHERE id = ?", (label, wallet_id))
    if cat_id is None:
        return
    cat = conn.execute("SELECT name, parent_id FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if cat is None or fold(cat[0]) != fold(old_label):
        return
    siblings = conn.execute("SELECT name FROM categories WHERE parent_id IS ? AND id != ?",
                            (cat[1], cat_id)).fetchall()
    if any(fold(n) == fold(label) for (n,) in siblings):
        return
    conn.execute("UPDATE categories SET name = ? WHERE id = ?", (label, cat_id))


def remove_wallet(conn: sqlite3.Connection, wallet_id: int) -> None:
    conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))     # snapshots et positions en cascade


def set_validator_label(conn: sqlite3.Connection, vote_account: str, label: str | None) -> None:
    label = (label or "").strip()
    if not label:
        conn.execute("DELETE FROM validator_labels WHERE vote_account = ?", (vote_account,))
        return
    conn.execute("INSERT INTO validator_labels(vote_account, label) VALUES (?, ?) "
                 "ON CONFLICT(vote_account) DO UPDATE SET label = excluded.label", (vote_account, label))


def hide_mint(conn: sqlite3.Connection, mint: str, hidden: bool) -> None:
    if hidden:
        conn.execute("INSERT OR IGNORE INTO hidden_mints(mint) VALUES (?)", (mint,))
    else:
        conn.execute("DELETE FROM hidden_mints WHERE mint = ?", (mint,))


def hidden_mints(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(m for (m,) in conn.execute("SELECT mint FROM hidden_mints"))


def record_reading(conn: sqlite3.Connection, wallet_id: int, taken_at: str, reading: WalletReading) -> int:
    snap = conn.execute("INSERT INTO wallet_snapshots(wallet_id, taken_at, status, total_eur_cents, unpriced_count) "
                        "VALUES (?, ?, 'ok', ?, ?)",
                        (wallet_id, taken_at, reading.total_eur_cents, reading.unpriced_count)).lastrowid
    conn.executemany(
        "INSERT INTO wallet_positions(snapshot_id, kind, mint, symbol, name, amount, decimals, price_eur,"
        " price_source, value_eur_cents, stake_account, vote_account, stake_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(snap, p.kind, p.mint, p.symbol, p.name, str(p.amount), p.decimals,
          None if p.price_eur is None else str(p.price_eur), p.price_source, p.value_eur_cents,
          p.stake_account, p.vote_account, p.stake_state) for p in reading.positions])
    conn.execute("UPDATE wallets SET last_sync_at = ?, last_error = NULL WHERE id = ?", (taken_at, wallet_id))
    return snap


def record_error(conn: sqlite3.Connection, wallet_id: int, taken_at: str, message: str) -> int:
    message = message[:ERROR_MAX]
    snap = conn.execute("INSERT INTO wallet_snapshots(wallet_id, taken_at, status, message) VALUES (?, ?, 'error', ?)",
                        (wallet_id, taken_at, message)).lastrowid
    conn.execute("UPDATE wallets SET last_error = ? WHERE id = ?", (message, wallet_id))
    return snap


def _net_deposited(conn: sqlite3.Connection, category_id: int | None) -> tuple[Decimal, Decimal, Decimal]:
    """(versements, retraits, net versé) en euros, sur tous les comptes, jambes miroirs exclues."""
    if category_id is None:
        return Decimal(0), Decimal(0), Decimal(0)
    out, inc = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents END), 0),"
        " COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents END), 0)"
        " FROM transactions WHERE category_id = ? AND mirror_of IS NULL", (category_id,)).fetchone()
    return from_cents(out), from_cents(inc), from_cents(out - inc)


def _positions(conn: sqlite3.Connection, snapshot_id: int, hidden: frozenset[str],
               validators: dict[str, str]) -> tuple[list[dict], int, int]:
    """Positions du relevé, avec le total et le nombre de positions sans prix, hors jetons masqués."""
    positions, total_cents, unpriced = [], 0, 0
    for (kind, mint, symbol, name, amount, decimals, price, source, cents, stake_account, vote_account,
         state) in conn.execute(
            "SELECT kind, mint, symbol, name, amount, decimals, price_eur, price_source, value_eur_cents,"
            " stake_account, vote_account, stake_state FROM wallet_positions WHERE snapshot_id = ? ORDER BY id",
            (snapshot_id,)):
        is_hidden = mint is not None and mint in hidden
        positions.append({"kind": kind, "mint": mint, "symbol": symbol, "name": name, "amount": Decimal(amount),
                          "decimals": decimals, "price_eur": None if price is None else Decimal(price),
                          "price_source": source, "value_eur": None if cents is None else from_cents(cents),
                          "stake_account": stake_account, "vote_account": vote_account, "stake_state": state,
                          "hidden": is_hidden,
                          "validator_label": validators.get(vote_account) if vote_account else None})
        if is_hidden:
            continue
        if cents is None:
            unpriced += 1
        else:
            total_cents += cents
    return positions, total_cents, unpriced


def overview(conn: sqlite3.Connection) -> dict:
    hidden = hidden_mints(conn)
    validators = dict(conn.execute("SELECT vote_account, label FROM validator_labels").fetchall())
    wallets = []
    for wid, label, address, cat_id, last_sync, last_error in conn.execute(
            "SELECT id, label, address, savings_category_id, last_sync_at, last_error FROM wallets ORDER BY id"
    ).fetchall():
        cat_name = None
        if cat_id is not None:
            row = conn.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)).fetchone()
            cat_name = row[0] if row else None
        deposits, withdrawals, net = _net_deposited(conn, cat_id)
        snap = conn.execute("SELECT id FROM wallet_snapshots WHERE wallet_id = ? AND status = 'ok' "
                            "ORDER BY taken_at DESC, id DESC LIMIT 1", (wid,)).fetchone()
        positions, total_cents, unpriced = _positions(conn, snap[0], hidden, validators) if snap else ([], 0, 0)
        history = [{"taken_at": t, "total_eur": from_cents(c)} for t, c in conn.execute(
            "SELECT taken_at, total_eur_cents FROM wallet_snapshots WHERE wallet_id = ? AND status = 'ok' "
            "ORDER BY taken_at, id", (wid,)).fetchall()]
        wallets.append({"id": wid, "label": label, "address": address, "category_id": cat_id,
                        "category_name": cat_name, "net_deposited": net, "deposits": deposits,
                        "withdrawals": withdrawals, "total_eur": from_cents(total_cents),
                        "unpriced_count": unpriced, "last_sync_at": last_sync, "last_error": last_error,
                        "positions": positions, "history": history})
    last_refresh = conn.execute("SELECT MAX(taken_at) FROM wallet_snapshots WHERE status = 'ok'").fetchone()[0]
    return {"total_eur": sum((x["total_eur"] for x in wallets), Decimal(0)),
            "unpriced_count": sum(x["unpriced_count"] for x in wallets), "last_refresh": last_refresh,
            "wallets": wallets, "validators": validators, "hidden_mints": sorted(hidden)}
