"""Onglet « Vue d'ensemble » : totaux, répartition, évolution mensuelle, hausses et récurrents."""
import sqlite3
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends

from app.analytics.periods import previous_period
from app.analytics.recurring import recurring
from app.analytics.totals import by_category, monthly_series, top_increases, totals
from app.deps import get_conn
from app.tabs._params import Scope, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

router = APIRouter()


@router.get("/summary")
def summary(conn: Annotated[sqlite3.Connection, Depends(get_conn)], s: Annotated[Scope, Depends(scope)]):
    p, accounts = s.period, s.account_ids
    reference = s.compare or previous_period(p)     # les hausses se mesurent toujours par rapport à une période
    return to_jsonable({
        "totals": asdict(totals(conn, p, accounts)),
        "compare_totals": None if s.compare is None else asdict(totals(conn, s.compare, accounts)),
        "by_category": [{"id": cid, "name": name, "amount": amount}
                        for cid, name, amount in by_category(conn, p, accounts, "expense", "parent")],
        "monthly": monthly_series(conn, p, accounts),
        "top_increases": [{"name": name, "increase": delta}
                          for name, delta in top_increases(conn, p, reference, accounts)],
        "recurring": recurring(conn, p, accounts),
    })


TAB = Tab("overview", "Vue d'ensemble", 10, router)
