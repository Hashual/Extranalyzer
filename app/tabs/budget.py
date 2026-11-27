"""Onglet « Budget » : objectifs mensuels par catégorie racine de dépense (datés : un changement vaut à partir
d'un mois), suivi de la période (dépensé, attendu à ce jour, projection), assistant de proposition d'objectifs,
ce qui coûte cher et conseils. Calculs dans `app.analytics.budget` ; montants en euros dans le JSON."""
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.analytics import budget
from app.deps import get_conn
from app.importer.amounts import from_cents, to_cents
from app.tabs._params import Scope, _accounts, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

MAX_AMOUNT = Decimal("1000000")

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

# Clés dont la valeur numérique est en centimes (converties en euros dans le JSON).
_CENTS = {"target", "spent", "expected", "projection", "monthly_target", "spent_budgeted", "spent_unbudgeted",
          "per_day", "spent_all", "average", "suggested", "total", "projected_savings", "income", "income_average",
          "savings", "unclassified_average", "amount", "previous", "monthly", "yearly", "subscriptions_yearly",
          "expenses", "per_month"}


def _euros(node, key=None):
    if isinstance(node, dict):
        return {k: _euros(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_euros(v, key) for v in node]
    if key in _CENTS and isinstance(node, (int, float)) and not isinstance(node, bool):
        return from_cents(int(round(node)))
    return node


def _today() -> date:
    return date.today()


def _month(raw: str | None) -> str:
    month = raw or budget.month_of(_today())
    if not budget.is_month(month):
        raise HTTPException(422, "mois attendu au format AAAA-MM")
    return month


def _cents(raw: str | None, what: str = "montant") -> int | None:
    """Montant en euros (« 350 », « 350,50 », « 350.5 ») -> centimes ; vide ou None -> None."""
    if raw is None or not str(raw).strip():
        return None
    text = str(raw)
    for junk in (" ", "\xa0", " ", "€"):
        text = text.replace(junk, "")
    text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise HTTPException(422, f"{what} invalide : {raw!r}") from None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT or value != value.quantize(Decimal("0.01")):
        raise HTTPException(422, f"{what} invalide : {raw!r} (positif, au centime près)")
    return to_cents(value)


@router.get("/summary")
def summary(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    return to_jsonable(_euros(budget.summary(conn, s.period, s.account_ids, _today())))


@router.get("/insights")
def insights(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    return to_jsonable(_euros(budget.insights(conn, s.period, s.account_ids, _today())))


def _targets_payload(conn: sqlite3.Connection, month: str) -> dict:
    return to_jsonable(_euros({"month": month, "current_month": budget.month_of(_today()),
                               "categories": budget.targets_for(conn, month), "changes": budget.changes(conn)}))


@router.get("/targets")
def get_targets(conn: Conn, month: str | None = None):
    return _targets_payload(conn, _month(month))


class TargetValue(BaseModel):
    category_id: int
    amount: str | None = None          # euros ; vide ou absent : pas d'objectif


class TargetsBody(BaseModel):
    from_month: str | None = None      # AAAA-MM ; par défaut le mois courant
    targets: list[TargetValue]


@router.put("/targets")
def put_targets(body: TargetsBody, conn: Conn):
    m = _month(body.from_month)
    values = {t.category_id: _cents(t.amount) for t in body.targets}
    try:
        with conn:
            changed = budget.save_targets(conn, m, values, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {**_targets_payload(conn, m), "changed": changed}


@router.get("/suggest")
def suggest(conn: Conn, accounts: str | None = Query(None), income: str | None = None, savings: str | None = None,
            effort: Literal["light", "medium", "strong"] = "medium"):
    return to_jsonable(_euros(budget.suggest(conn, _accounts(accounts), _today(), income=_cents(income, "revenu"),
                                             savings=_cents(savings, "épargne") or 0, effort=effort)))


TAB = Tab("budget", "Budget", 15, router)
