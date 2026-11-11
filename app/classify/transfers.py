"""Appariement des virements internes entre compte courant et compte d'épargne.

Ne valide pas : l'appelant possède la transaction.
"""
import sqlite3
from datetime import date

from app.classify.rules import sync_mirrors
from app.classify.savings_target import savings_category_for_account

MAX_GAP_DAYS = 5


def pair_internal_transfers(conn: sqlite3.Connection) -> int:
    """Apparie les virements internes ; renvoie le nombre de paires créées.

    La jambe côté compte `savings` reçoit `mirror_of` = id de l'autre jambe et sa catégorie.
    """
    rows = conn.execute(
        "SELECT t.id, t.account_id, a.kind, t.op_date, t.amount_cents FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id LEFT JOIN sections s ON s.id = t.section_id "
        "WHERE t.mirror_of IS NULL AND (s.id IS NULL OR s.status = 'ok') "
        "AND t.id NOT IN (SELECT mirror_of FROM transactions WHERE mirror_of IS NOT NULL) "
        "ORDER BY t.op_date, t.id").fetchall()
    by_amount: dict[int, list[tuple]] = {}
    for r in rows:
        if r[4] != 0:
            by_amount.setdefault(abs(r[4]), []).append(r)
    used: set[int] = set()
    pairs = 0
    for tx_id, account_id, kind, op_date, amount in rows:
        if tx_id in used or amount == 0:
            continue
        day = date.fromisoformat(op_date)
        best = None                                   # (écart en jours, id)
        for o_id, o_account, o_kind, o_date, o_amount in by_amount[abs(amount)]:
            if (o_id == tx_id or o_id in used or o_account == account_id or o_amount != -amount
                    or (kind == "savings") == (o_kind == "savings")):
                continue
            gap = abs((date.fromisoformat(o_date) - day).days)
            if gap <= MAX_GAP_DAYS and (best is None or (gap, o_id) < best):
                best = (gap, o_id)
        if best is None:
            continue
        other_id = best[1]
        mirror_id, source_id = (tx_id, other_id) if kind == "savings" else (other_id, tx_id)
        conn.execute(
            "UPDATE transactions SET mirror_of = ?, rule_id = NULL, "
            "category_id = (SELECT category_id FROM transactions WHERE id = ?), "
            "classified_by = CASE WHEN (SELECT category_id FROM transactions WHERE id = ?) IS NULL "
            "THEN NULL ELSE 'pair' END WHERE id = ?",
            (source_id, source_id, source_id, mirror_id))
        used.update((tx_id, other_id))
        pairs += 1
    return pairs


def classify_paired_sources(conn: sqlite3.Connection) -> int:
    """Classe en Épargne / <livret> les jambes sources appariées non manuelles, sans catégorie ou classées
    par le modèle (appariement > modèle).

    Une source déjà classée (règle) garde sa catégorie ; seules les jambes miroir portées par un
    compte d'épargne sont concernées. Renvoie le nombre de sources classées.
    """
    rows = conn.execute(
        "SELECT t.id, m.account_id FROM transactions t JOIN transactions m ON m.mirror_of = t.id "
        "JOIN accounts a ON a.id = m.account_id "
        "WHERE (t.category_id IS NULL OR t.classified_by = 'model') AND t.manual = 0 AND a.kind = 'savings' "
        "ORDER BY t.id").fetchall()
    cache: dict[int, int] = {}
    for tx_id, account_id in rows:
        if account_id not in cache:
            cache[account_id] = savings_category_for_account(conn, account_id)
        conn.execute("UPDATE transactions SET category_id = ?, rule_id = NULL, classified_by = 'pair', "
                     "confidence = NULL, suggested_category_id = NULL, suggestion_reason = NULL, "
                     "suggestion_support = NULL WHERE id = ?", (cache[account_id], tx_id))
    sync_mirrors(conn, [r[0] for r in rows])
    return len(rows)
