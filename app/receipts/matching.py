"""Rapprochement des tickets de caisse avec les débits carte des comptes courants.

Aucune fonction ne valide : l'appelant possède la transaction."""
import re
import sqlite3
from collections import Counter

from app.importer.amounts import from_cents
from app.store.repo import fold


def card_cents(conn: sqlite3.Connection, receipt_id: int) -> int:
    """Part du ticket réglée par carte bancaire, en centimes."""
    return conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM receipt_payments "
                        "WHERE receipt_id = ? AND method = 'card'", (receipt_id,)).fetchone()[0]


def _tokens(text: str) -> set[str]:
    return set(re.split(r"[^A-Z0-9]+", fold(text))) - {""}


def _tiebreak(rows: list[tuple], shop: str | None) -> list[tuple]:
    """Parmi des débits de même montant : ceux dont le libellé cite le jeton `U` ou un mot du magasin."""
    wanted = {"U"} | {w for w in _tokens(shop or "") if len(w) >= 3}
    return [r for r in rows if _tokens(r[1]) & wanted]


def match_receipts(conn: sqlite3.Connection) -> dict:
    linked = unlinked = broken = 0
    # 1. liens dont le débit a disparu
    for r in conn.execute("SELECT id, link_mode, link_broken FROM receipts WHERE tx_key IS NOT NULL "
                          "AND tx_key NOT IN (SELECT tx_key FROM transactions)").fetchall():
        if r[1] == "auto":
            conn.execute("UPDATE receipts SET tx_key = NULL, link_mode = NULL WHERE id = ?", (r[0],))
            unlinked += 1
        elif not r[2]:
            conn.execute("UPDATE receipts SET link_broken = 1 WHERE id = ?", (r[0],))
            broken += 1
    # lien manuel cassé dont le débit est revenu (relevé réimporté)
    conn.execute("UPDATE receipts SET link_broken = 0 WHERE link_broken = 1 AND link_mode = 'manual' "
                 "AND tx_key IN (SELECT tx_key FROM transactions)")
    # 2. liens auto dont le montant ne correspond plus
    for r in conn.execute("SELECT r.id, t.amount_cents FROM receipts r JOIN transactions t ON t.tx_key = r.tx_key "
                          "WHERE r.link_mode = 'auto'").fetchall():
        if r[1] != -card_cents(conn, r[0]):
            conn.execute("UPDATE receipts SET tx_key = NULL, link_mode = NULL WHERE id = ?", (r[0],))
            unlinked += 1
    # 3. nouveaux liens : tous les candidats sont évalués sur un même instantané des liens existants ;
    # un débit visé par plusieurs tickets est disputé, aucun d'eux n'est relié (jamais au hasard).
    todo = conn.execute("SELECT id, shop, purchased_at FROM receipts WHERE status = 'ok' AND tx_key IS NULL "
                        "AND link_broken = 0 AND link_mode IS NULL AND purchased_at IS NOT NULL "
                        "ORDER BY purchased_at, id").fetchall()
    taken = {r[0] for r in conn.execute("SELECT tx_key FROM receipts WHERE tx_key IS NOT NULL")}
    chosen: dict[int, str] = {}
    for rid, shop, purchased_at in todo:
        cents = card_cents(conn, rid)
        if cents <= 0:
            continue
        rows = [r for r in conn.execute(
            "SELECT t.tx_key, t.label_norm FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.kind = 'checking' AND t.mirror_of IS NULL AND t.amount_cents = ? "
            "AND t.op_date BETWEEN date(?) AND date(?, '+3 days') ORDER BY t.op_date, t.id",
            (-cents, purchased_at, purchased_at)) if r[0] not in taken]
        if len(rows) > 1:
            rows = _tiebreak(rows, shop)
        if len(rows) == 1:
            chosen[rid] = rows[0][0]
    votes = Counter(chosen.values())
    for rid, key in chosen.items():
        if votes[key] == 1:
            conn.execute("UPDATE receipts SET tx_key = ?, link_mode = 'auto', link_broken = 0 WHERE id = ?",
                         (key, rid))
            linked += 1
    return {"linked": linked, "unlinked": unlinked, "broken": broken}


def link_manual(conn: sqlite3.Connection, receipt_id: int, tx_key: str | None) -> None:
    """Relie à la main le ticket au débit `tx_key`, ou le délie avec `None`."""
    if tx_key is None:
        # Déliaison explicite : le mode 'manual' sans débit empêche tout nouveau lien automatique.
        conn.execute("UPDATE receipts SET tx_key = NULL, link_mode = 'manual', link_broken = 0 WHERE id = ?",
                     (receipt_id,))
        return
    if conn.execute("SELECT 1 FROM receipts WHERE tx_key = ? AND id != ?", (tx_key, receipt_id)).fetchone():
        raise ValueError("ce débit est déjà relié à un autre ticket")
    conn.execute("UPDATE receipts SET tx_key = ?, link_mode = 'manual', link_broken = 0 WHERE id = ?",
                 (tx_key, receipt_id))


def candidates(conn: sqlite3.Connection, receipt_id: int, limit: int = 30) -> list[dict]:
    """Débits proposés pour une liaison manuelle : même montant à ±7 jours d'abord, puis tout débit
    des comptes courants à ±7 jours, du plus proche au plus lointain."""
    row = conn.execute("SELECT purchased_at FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if row is None or row[0] is None:
        return []
    cents = card_cents(conn, receipt_id)
    rows = conn.execute(
        "SELECT t.tx_key, t.op_date, t.label, t.amount_cents FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        "WHERE a.kind = 'checking' AND t.mirror_of IS NULL AND t.amount_cents < 0 "
        "AND t.op_date BETWEEN date(?, '-7 days') AND date(?, '+7 days') "
        "ORDER BY (t.amount_cents = ?) DESC, ABS(julianday(t.op_date) - julianday(date(?))), t.id LIMIT ?",
        (row[0], row[0], -cents, row[0], limit)).fetchall()
    return [{"tx_key": r[0], "op_date": r[1], "label": r[2], "amount": from_cents(r[3])} for r in rows]
