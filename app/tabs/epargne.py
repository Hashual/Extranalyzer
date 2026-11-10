"""Onglet « Épargne » : versements, retraits et solde net par destination, et cumul mensuel.

Convention de signe et périmètre : ceux de `app.analytics.totals` (SIGNED_AMOUNT, scope_clause) ;
jambes miroir exclues.
"""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from app.analytics.periods import month_keys
from app.analytics.totals import SIGNED_AMOUNT, scope_clause
from app.deps import get_conn
from app.importer.amounts import from_cents
from app.tabs._params import Scope, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

SIGNED = SIGNED_AMOUNT["savings"]

router = APIRouter()


def _flows(deposits: int, withdrawals: int) -> dict:
    return {"deposits": from_cents(deposits), "withdrawals": from_cents(withdrawals),
            "net": from_cents(deposits - withdrawals)}


@router.get("/summary")
def summary(conn: Annotated[sqlite3.Connection, Depends(get_conn)], s: Annotated[Scope, Depends(scope)]):
    where, params = scope_clause(s.period, s.account_ids)
    flows = {cid: (dep, wd) for cid, dep, wd in conn.execute(
        f"""SELECT t.category_id,
                   COALESCE(SUM(CASE WHEN {SIGNED} > 0 THEN {SIGNED} END), 0),
                   COALESCE(SUM(CASE WHEN {SIGNED} < 0 THEN -({SIGNED}) END), 0)
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            JOIN categories c ON c.id = t.category_id
            WHERE {where} AND c.kind = 'savings' GROUP BY 1""", params)}
    # Destinations : sous-catégories des racines « épargne », plus une racine si des opérations y sont
    # classées directement.
    categories = conn.execute(
        """SELECT c.id, c.name, p.name FROM categories c LEFT JOIN categories p ON p.id = c.parent_id
           WHERE c.kind = 'savings' ORDER BY c.parent_id IS NOT NULL, c.name""").fetchall()
    destinations = []
    for cid, name, parent_name in categories:
        if parent_name is None and cid not in flows:
            continue
        label = name if parent_name is not None else f"{name} (sans destination)"
        destinations.append({"id": cid, "name": label, **_flows(*flows.get(cid, (0, 0)))})
    destinations.sort(key=lambda d: (-d["net"], d["name"]))

    per_month = dict(conn.execute(
        f"""SELECT substr(t.op_date, 1, 7), SUM({SIGNED})
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            JOIN categories c ON c.id = t.category_id
            WHERE {where} AND c.kind = 'savings' GROUP BY 1""", params).fetchall())
    monthly, cumulative = [], 0
    for month in month_keys(s.period):
        cumulative += per_month.get(month, 0)
        monthly.append({"month": month, "net": from_cents(per_month.get(month, 0)),
                        "net_cumulative": from_cents(cumulative)})

    total_dep = sum(dep for dep, _ in flows.values())
    total_wd = sum(wd for _, wd in flows.values())
    return to_jsonable({"destinations": destinations, "total": _flows(total_dep, total_wd), "monthly": monthly})


TAB = Tab("epargne", "Épargne", 40, router)
