"""Onglet « À classer » : opérations sans catégorie regroupées par libellé normalisé, et classement d'un
groupe (par une nouvelle règle, ou à la main opération par opération).

Périmètre : celui de la barre (période, comptes) ; jambes miroir exclues (elles suivent leur source).
"""
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.analytics.totals import scope_clause, unclassified_count
from app.classify.pipeline import reclassify
from app.classify.rules import create_rule, set_manual_category
from app.deps import get_conn
from app.importer.amounts import from_cents
from app.tabs._params import Scope, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

GROUP_FROM = "FROM transactions t JOIN accounts a ON a.id = t.account_id"


class ClassifyBody(BaseModel):
    label_norm: str
    category_id: int
    create_rule: bool = True
    pattern: str | None = None          # défaut : le libellé normalisé du groupe
    match: Literal["contains", "startswith", "regex"] = "contains"
    direction: Literal["debit", "credit"] | None = None


def _sign(low: int, high: int) -> str:
    if high < 0:
        return "debit"
    if low > 0:
        return "credit"
    return "mixed"


@router.get("/list")
def list_groups(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    where, params = scope_clause(s.period, s.account_ids)
    rows = conn.execute(
        f"""SELECT t.label_norm, COUNT(*), SUM(t.amount_cents), MIN(t.label), MIN(t.op_date), MAX(t.op_date),
                   MIN(t.amount_cents), MAX(t.amount_cents)
            {GROUP_FROM} WHERE {where} AND t.category_id IS NULL
            GROUP BY t.label_norm
            ORDER BY COUNT(*) DESC, ABS(SUM(t.amount_cents)) DESC, t.label_norm""", params).fetchall()
    groups = [{"label_norm": label_norm, "count": count, "total": from_cents(total), "sample_label": sample,
               "first_date": first, "last_date": last, "sign": _sign(low, high)}
              for label_norm, count, total, sample, first, last, low, high in rows]
    return to_jsonable({"groups": groups})


@router.post("/classify")
def classify(body: ClassifyBody, conn: Conn, s: Annotated[Scope, Depends(scope)]):
    if conn.execute("SELECT 1 FROM categories WHERE id = ?", (body.category_id,)).fetchone() is None:
        raise HTTPException(404, f"catégorie {body.category_id} introuvable")
    where, params = scope_clause(s.period, s.account_ids)
    tx_ids = [row[0] for row in conn.execute(
        f"SELECT t.id {GROUP_FROM} WHERE {where} AND t.category_id IS NULL AND t.label_norm = ?",
        params + [body.label_norm])]
    if not tx_ids:
        raise HTTPException(404, "aucune opération à classer pour ce libellé sur la période")

    rule_id = None
    with conn:          # valide si tout réussit, annule sinon (HTTPException comprise)
        if body.create_rule:
            pattern = (body.pattern if body.pattern is not None else body.label_norm).strip()
            try:
                # La règle classe toutes les opérations encore à classer qu'elle reconnaît, hors période comprise.
                rule_id = create_rule(conn, pattern, body.match, body.category_id, direction=body.direction,
                                      apply_to="unclassified")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            in_group = conn.execute(
                f"SELECT COUNT(*) FROM transactions WHERE rule_id = ? AND id IN ({','.join('?' * len(tx_ids))})",
                [rule_id, *tx_ids]).fetchone()[0]
            if in_group == 0:
                raise HTTPException(422, "le motif ne reconnaît aucune opération de ce groupe")
            classified = conn.execute("SELECT COUNT(*) FROM transactions WHERE rule_id = ? AND mirror_of IS NULL",
                                      (rule_id,)).fetchone()[0]
        else:
            for tx_id in tx_ids:
                set_manual_category(conn, tx_id, body.category_id, reclassify_after=False)
            reclassify(conn)                    # un seul passage de la chaîne pour tout le groupe
            classified = len(tx_ids)
    return {"classified": classified, "rule_id": rule_id, "unclassified_count": unclassified_count(conn)}


TAB = Tab("a_classer", "À classer", 50, router)
