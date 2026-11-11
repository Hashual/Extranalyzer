"""Chaîne de classement unique : règles → appariement des virements → modèle → synchronisation des miroirs.

Tout classement autre que `manual` est un état dérivé, recalculé sur toutes les lignes à chaque passage.
Ne valide pas : l'appelant possède la transaction.
"""
import sqlite3

from app.classify.model import Example, build_index, predict
from app.classify.rules import apply_rules, sync_mirrors
from app.classify.transfers import classify_paired_sources, pair_internal_transfers
from app.config import auto_threshold

EXAMPLE_SOURCES = ("manual", "rule", "pair")


def _apply_model(conn: sqlite3.Connection, threshold: float) -> tuple[int, int, int]:
    """Classe ou suggère chaque ligne non miroir non classée (ou classée `model`).

    Renvoie (classées par le modèle, suggestions, sans suggestion).
    """
    rows = conn.execute(
        "SELECT id, label_norm, amount_cents, account_id, category_id, classified_by, manual FROM transactions "
        "WHERE mirror_of IS NULL ORDER BY id").fetchall()
    # Exemples triés par id : le départage des égalités dépend de leur ordre.
    examples = [Example(label, amount, account, category) for _, label, amount, account, category, by, _ in rows
                if by in EXAMPLE_SOURCES]
    index = build_index(examples)
    updates = []
    auto = suggested = unclassified = 0
    for tx_id, label, amount, account, _, by, is_manual in rows:
        if is_manual or by not in (None, "model"):
            continue
        p = predict(index, label, amount, account)
        if p.is_auto(threshold):
            updates.append((p.category_id, "model", p.confidence, p.category_id, p.reason, tx_id))
            auto += 1
        else:
            confidence = None if p.category_id is None else p.confidence
            updates.append((None, None, confidence, p.category_id, p.reason, tx_id))
            if p.category_id is None:
                unclassified += 1
            else:
                suggested += 1
    conn.executemany("UPDATE transactions SET category_id = ?, rule_id = NULL, classified_by = ?, confidence = ?, "
                     "suggested_category_id = ?, suggestion_reason = ? WHERE id = ?", updates)
    return auto, suggested, unclassified


def reclassify(conn: sqlite3.Connection, threshold: float | None = None) -> dict[str, int]:
    """Repasse toute la chaîne sur toutes les lignes ; ne valide pas.

    `threshold` None : EXTRANALYZER_AUTO_THRESHOLD, sinon 0,90. Les compteurs portent sur les lignes non miroir.
    """
    if threshold is None:
        threshold = auto_threshold()
    apply_rules(conn)
    pair_internal_transfers(conn)
    classify_paired_sources(conn)
    model, suggested, unclassified = _apply_model(conn, threshold)
    conn.execute("UPDATE transactions SET confidence = NULL, suggested_category_id = NULL, suggestion_reason = NULL "
                 "WHERE classified_by IN ('manual', 'rule', 'pair') AND mirror_of IS NULL")
    sync_mirrors(conn)
    counts = dict(conn.execute(
        "SELECT classified_by, COUNT(*) FROM transactions WHERE mirror_of IS NULL "
        "AND classified_by IN ('rule', 'pair') GROUP BY classified_by").fetchall())
    return {"rule": counts.get("rule", 0), "pair": counts.get("pair", 0), "model": model,
            "suggested": suggested, "unclassified": unclassified}


def on_new_transactions(conn: sqlite3.Connection, new_ids: list[int]) -> None:
    """Rappel de l'import : `new_ids` est gardé pour le contrat du service, la chaîne repasse sur tout."""
    reclassify(conn)
