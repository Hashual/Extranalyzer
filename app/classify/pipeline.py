"""Chaîne de classement unique : règles → appariement des virements → modèle → synchronisation des miroirs.

Tout classement autre que `manual` est un état dérivé, recalculé sur toutes les lignes à chaque passage.
Ne valide pas : l'appelant possède la transaction.
"""
import sqlite3

from app.classify.model import Example, Prediction, build_index, predict, prediction_key
from app.classify.rules import apply_rules, sync_mirrors
from app.classify.transfers import classify_paired_sources, pair_internal_transfers
from app.config import auto_threshold

# Les lignes `pair` ne sont pas des exemples : les virements internes sont déjà traités par l'appariement et
# tireraient les suggestions vers Épargne. Tout exemple est donc de confiance (`trusted` reste une défense).
EXAMPLE_SOURCES = ("manual", "rule")
TRUSTED_SOURCES = ("manual", "rule")


def _apply_model(conn: sqlite3.Connection, threshold: float) -> tuple[int, int, int]:
    """Classe ou suggère chaque ligne non miroir non classée (ou classée `model`).

    N'écrit que les lignes dont l'état change. Renvoie (classées par le modèle, suggestions, sans suggestion).
    """
    rows = conn.execute(
        "SELECT id, label_norm, amount_cents, account_id, category_id, classified_by, manual, rule_id, confidence, "
        "suggested_category_id, suggestion_reason, suggestion_support FROM transactions "
        "WHERE mirror_of IS NULL ORDER BY id").fetchall()
    # Exemples triés par id : le départage des égalités dépend de leur ordre.
    examples = [Example(r[1], r[2], r[3], r[4], trusted=r[5] in TRUSTED_SOURCES) for r in rows
                if r[5] in EXAMPLE_SOURCES]
    index = build_index(examples)
    memo: dict[tuple, Prediction] = {}
    updates = []
    auto = suggested = unclassified = 0
    for tx_id, label, amount, account, category, by, is_manual, *current in rows:
        if is_manual or by not in (None, "model"):
            continue
        key = prediction_key(label, amount, account)
        p = memo.get(key)
        if p is None:
            p = memo[key] = predict(index, label, amount, account)
        if p.is_auto(threshold):
            new = (p.category_id, "model", None, p.confidence, p.category_id, p.reason, p.support)
            auto += 1
        elif p.category_id is None:
            new = (None, None, None, None, None, p.reason, None)
            unclassified += 1
        else:
            new = (None, None, None, p.confidence, p.category_id, p.reason, p.support)
            suggested += 1
        if new != (category, by, *current):
            updates.append((*new, tx_id))
    conn.executemany("UPDATE transactions SET category_id = ?, classified_by = ?, rule_id = ?, confidence = ?, "
                     "suggested_category_id = ?, suggestion_reason = ?, suggestion_support = ? WHERE id = ?",
                     updates)
    return auto, suggested, unclassified


def reclassify(conn: sqlite3.Connection, threshold: float | None = None) -> dict[str, int]:
    """Repasse toute la chaîne sur toutes les lignes ; ne valide pas.

    `threshold` None : EXTRANALYZER_AUTO_THRESHOLD, sinon 0,90. Les compteurs portent sur les lignes non miroir.
    """
    if threshold is None:
        threshold = auto_threshold()
    elif not 0 < threshold <= 1:
        raise ValueError(f"seuil hors de ]0, 1] : {threshold!r}")
    apply_rules(conn)
    pair_internal_transfers(conn)
    classify_paired_sources(conn)
    model, suggested, unclassified = _apply_model(conn, threshold)
    conn.execute("UPDATE transactions SET confidence = NULL, suggested_category_id = NULL, suggestion_reason = NULL, "
                 "suggestion_support = NULL WHERE classified_by IN ('manual', 'rule', 'pair') AND mirror_of IS NULL "
                 "AND (confidence IS NOT NULL OR suggested_category_id IS NOT NULL OR suggestion_reason IS NOT NULL "
                 "OR suggestion_support IS NOT NULL)")
    sync_mirrors(conn)
    counts = dict(conn.execute(
        "SELECT classified_by, COUNT(*) FROM transactions WHERE mirror_of IS NULL "
        "AND classified_by IN ('rule', 'pair') GROUP BY classified_by").fetchall())
    return {"rule": counts.get("rule", 0), "pair": counts.get("pair", 0), "model": model,
            "suggested": suggested, "unclassified": unclassified}


def on_new_transactions(conn: sqlite3.Connection, new_ids: list[int]) -> None:
    """Rappel de l'import : `new_ids` est gardé pour le contrat du service, la chaîne repasse sur tout."""
    reclassify(conn)
