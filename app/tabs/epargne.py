"""Onglet « Épargne » : versements, retraits et solde net par destination, et cumul mensuel.

Convention de signe, nature et périmètre : ceux de `app.analytics.totals` (SIGNED_AMOUNT, EFFECTIVE_KIND,
scope_clause) ; jambes miroir exclues. Les virements internes appariés encore non classés forment une
destination sans id.
"""
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.analytics import holdings
from app.analytics.periods import month_keys
from app.analytics.totals import ANALYSIS_FROM, EFFECTIVE_KIND, INTERNAL_TRANSFERS, SIGNED_AMOUNT, scope_clause
from app.deps import get_conn
from app.importer.amounts import from_cents, to_cents
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


class BalanceBody(BaseModel):
    amount: str                     # euros, ex. "12345.67" ou "12 345,67"
    day: date | None = None         # par défaut : aujourd'hui


def _savings_account(conn: sqlite3.Connection, account_id: int) -> None:
    if conn.execute("SELECT 1 FROM accounts WHERE id = ? AND kind = 'savings'", (account_id,)).fetchone() is None:
        raise HTTPException(404, "compte d'épargne introuvable")


def _amount_cents(raw: str) -> int:
    """Montant en euros saisi à la française : « 12345,67 », « 12 345,67 », « 12.345,67 » ou « 12345.67 ».

    Un point suivi d'exactement trois chiffres sans virgule (« 1.234 ») est ambigu : refusé."""
    text = raw.replace(" ", "").replace("\xa0", "").replace(" ", "").replace("€", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")          # point = séparateur de milliers
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        raise HTTPException(422, "montant ambigu : utilisez une virgule pour les centimes (ex. 1234,00)")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise HTTPException(422, "montant invalide : saisissez par exemple 12345,67") from None
    if not value.is_finite() or value < 0 or value > Decimal("100000000"):
        raise HTTPException(422, "montant invalide : saisissez un solde positif")
    return to_cents(value)


@router.put("/balance/{account_id}")
def set_balance(account_id: int, body: BalanceBody, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    """Solde réel d'un livret à une date, saisi à la main : il fait foi (voir holdings._Ledger)."""
    _savings_account(conn, account_id)
    day = body.day or _today()
    if day > _today():
        raise HTTPException(422, "la date ne peut pas être dans le futur")
    last = conn.execute("SELECT MAX(statement_date) FROM sections WHERE account_id = ? AND status = 'ok'",
                        (account_id,)).fetchone()[0]
    if last is not None and day.isoformat() < last:
        raise HTTPException(422, f"date antérieure au dernier relevé ({last}) : le relevé fait foi")
    with conn:
        holdings.set_manual_balance(conn, account_id, day, _amount_cents(body.amount),
                                    datetime.now(timezone.utc).isoformat())
    return {"ok": True}


@router.delete("/balance/{account_id}")
def clear_balance(account_id: int, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    _savings_account(conn, account_id)
    with conn:
        holdings.clear_manual_balance(conn, account_id)
    return {"ok": True}


TAB = Tab("epargne", "Épargne", 40, router)
