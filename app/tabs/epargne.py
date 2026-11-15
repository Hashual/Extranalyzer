"""Onglet « Épargne » : versements, retraits et solde net par destination, et cumul mensuel.

Convention de signe, nature et périmètre : ceux de `app.analytics.totals` (SIGNED_AMOUNT, EFFECTIVE_KIND,
scope_clause) ; jambes miroir exclues. Les virements internes appariés encore non classés forment une
destination sans id.
"""
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends

from app.analytics import holdings
from app.analytics.periods import month_keys
from app.analytics.totals import ANALYSIS_FROM, EFFECTIVE_KIND, INTERNAL_TRANSFERS, SIGNED_AMOUNT, scope_clause
from app.deps import get_conn
from app.importer.amounts import from_cents
from app.tabs._params import Scope, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

SIGNED = SIGNED_AMOUNT["savings"]
IS_SAVINGS = f"({EFFECTIVE_KIND}) = 'savings'"

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
            {ANALYSIS_FROM}
            WHERE {where} AND {IS_SAVINGS} GROUP BY 1""", params)}
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
    if None in flows:
        destinations.append({"id": None, "name": INTERNAL_TRANSFERS, **_flows(*flows[None])})
    destinations.sort(key=lambda d: (-d["net"], d["name"]))

    per_month = dict(conn.execute(
        f"""SELECT substr(t.op_date, 1, 7), SUM({SIGNED})
            {ANALYSIS_FROM}
            WHERE {where} AND {IS_SAVINGS} GROUP BY 1""", params).fetchall())
    monthly, cumulative = [], 0
    for month in month_keys(s.period):
        cumulative += per_month.get(month, 0)
        monthly.append({"month": month, "net": from_cents(per_month.get(month, 0)),
                        "net_cumulative": from_cents(cumulative)})

    total_dep = sum(dep for dep, _ in flows.values())
    total_wd = sum(wd for _, wd in flows.values())
    return to_jsonable({"destinations": destinations, "total": _flows(total_dep, total_wd), "monthly": monthly})


# Champs en centimes du tableau de bord ; les compteurs (count, unknown_count, ...) restent des entiers.
_CENTS = {"value", "change", "deposited", "gap", "compared_value", "savings", "crypto"}


def _euros(node, key=None):
    if isinstance(node, dict):
        return {k: _euros(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_euros(v, key) for v in node]
    return from_cents(node) if key in _CENTS and isinstance(node, int) and not isinstance(node, bool) else node


def _today() -> date:
    return date.today()


@router.get("/dashboard")
def dashboard(conn: Annotated[sqlite3.Connection, Depends(get_conn)], s: Annotated[Scope, Depends(scope)]):
    """Avoirs de tous les comptes d'épargne et portefeuilles ; le sélecteur de comptes est ignoré."""
    return to_jsonable(_euros(holdings.dashboard(conn, s.period, _today())))


TAB = Tab("epargne", "Épargne", 40, router)
