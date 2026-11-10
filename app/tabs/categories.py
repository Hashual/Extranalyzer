"""Onglet « Catégories » : montants par catégorie racine, puis détail d'une catégorie
(sous-catégories, évolution mensuelle, transactions) avec reclassement sur place."""
import sqlite3
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.analytics.periods import month_keys
from app.analytics.totals import SIGNED_AMOUNT, by_category, scope_clause, totals
from app.importer.amounts import from_cents
from app.deps import get_conn
from app.tabs._params import Scope, scope
from app.tabs._transactions import ROW_FROM, ROW_SELECT, ReclassBody, reclassify, row_to_dict
from app.tabs.registry import Tab
from app.web_json import to_jsonable

KINDS = ("expense", "income", "savings")
DETAIL_TX_LIMIT = 100        # transactions les plus récentes ; la suite est dans l'onglet Transactions
ZERO = Decimal("0.00")

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


@router.get("/tree")
def tree(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    p, accounts = s.period, s.account_ids
    parent_amounts, child_amounts = {}, {}
    for kind in KINDS:
        parent_amounts.update({cid: amt for cid, _, amt in by_category(conn, p, accounts, kind, "parent")
                               if cid is not None})
        child_amounts.update({cid: amt for cid, _, amt in by_category(conn, p, accounts, kind, "child")})
    rows = conn.execute("SELECT id, name, parent_id, kind FROM categories ORDER BY name").fetchall()
    roots = [{"id": cid, "name": name, "kind": kind, "amount": parent_amounts.get(cid, ZERO), "children": []}
             for cid, name, parent_id, kind in rows if parent_id is None]
    by_id = {r["id"]: r for r in roots}
    for cid, name, parent_id, _ in rows:
        if parent_id in by_id:
            by_id[parent_id]["children"].append({"id": cid, "name": name, "amount": child_amounts.get(cid, ZERO)})
    t = totals(conn, p, accounts)
    return to_jsonable({"roots": roots,
                        "unclassified": {"debits": t.unclassified_debits, "credits": t.unclassified_credits}})


@router.get("/{category_id}")
def detail(category_id: int, conn: Conn, s: Annotated[Scope, Depends(scope)]):
    row = conn.execute("SELECT id, name, parent_id, kind FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"catégorie {category_id} introuvable")
    _, name, parent_id, kind = row
    has_children = conn.execute("SELECT 1 FROM categories WHERE parent_id = ? LIMIT 1",
                                (category_id,)).fetchone() is not None
    children = []
    if has_children:
        children = [{"id": cid, "name": n, "amount": amt}
                    for cid, n, amt in by_category(conn, s.period, s.account_ids, kind, "child", category_id)]

    where, params = scope_clause(s.period, s.account_ids)
    where += " AND (t.category_id = ? OR c.parent_id = ?)"
    params += [category_id, category_id]
    per_month = dict(conn.execute(
        f"""SELECT substr(t.op_date, 1, 7), SUM({SIGNED_AMOUNT[kind]})
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            JOIN categories c ON c.id = t.category_id
            WHERE {where} GROUP BY 1""", params).fetchall())
    monthly = [{"month": m, "amount": from_cents(per_month.get(m, 0))} for m in month_keys(s.period)]
    transactions = conn.execute(f"{ROW_SELECT} WHERE {where} ORDER BY t.op_date DESC, t.id DESC LIMIT ?",
                                params + [DETAIL_TX_LIMIT]).fetchall()
    transactions_total = conn.execute(f"SELECT COUNT(*) {ROW_FROM} WHERE {where}", params).fetchone()[0]
    return to_jsonable({
        "id": category_id, "name": name, "parent_id": parent_id, "kind": kind,
        "amount": from_cents(sum(per_month.values())),
        "children": children, "monthly": monthly,
        "transactions": [row_to_dict(r) for r in transactions],
        "transactions_total": transactions_total,
    })


@router.post("/transactions/{tx_id}/category")
def set_category(tx_id: int, body: ReclassBody, conn: Conn):
    return to_jsonable(reclassify(conn, tx_id, body.category_id))


TAB = Tab("categories", "Catégories", 20, router)
