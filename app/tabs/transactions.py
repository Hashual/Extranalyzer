"""Onglet « Transactions » : tableau filtrable, triable, paginé, avec reclassement manuel."""
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.main import get_conn
from app.tabs._params import Scope, scope, scope_where
from app.tabs._transactions import ROW_FROM, ROW_SELECT, ReclassBody, reclassify, row_to_dict
from app.tabs.registry import Tab
from app.web_json import to_jsonable

PAGE_SIZE = 50
ORDER_BY = {
    "date_desc": "t.op_date DESC, t.id DESC",
    "date_asc": "t.op_date ASC, t.id ASC",
    "amount_asc": "t.amount_cents ASC, t.op_date DESC, t.id DESC",
    "amount_desc": "t.amount_cents DESC, t.op_date DESC, t.id DESC",
}

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _category_filter(raw: str | None) -> tuple[str, list]:
    if raw is None or raw == "":
        return "", []
    if raw == "none":
        return " AND t.category_id IS NULL", []
    if not raw.isdigit():
        raise HTTPException(422, "category : identifiant ou « none » attendu")
    cid = int(raw)       # une racine inclut ses sous-catégories
    return " AND (t.category_id = ? OR c.parent_id = ?)", [cid, cid]


@router.get("/list")
def list_transactions(conn: Conn, s: Annotated[Scope, Depends(scope)], q: str = "", category: str | None = None,
                      sort: Literal["date_desc", "date_asc", "amount_asc", "amount_desc"] = "date_desc",
                      page: Annotated[int, Query(ge=1, le=1_000_000)] = 1):
    where, params = scope_where(s, include_mirrors=True)
    extra, extra_params = _category_filter(category)
    where += extra
    params += extra_params
    if q.strip():
        # lower()/LIKE de SQLite n'ignorent la casse qu'en ASCII : minuscules calculées par Python.
        conn.create_function("py_lower", 1, str.lower, deterministic=True)
        where += " AND instr(py_lower(t.label), ?) > 0"
        params.append(q.strip().lower())
    total = conn.execute(f"SELECT COUNT(*) {ROW_FROM} WHERE {where}", params).fetchone()[0]
    rows = conn.execute(f"{ROW_SELECT} WHERE {where} ORDER BY {ORDER_BY[sort]} LIMIT ? OFFSET ?",
                        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    return to_jsonable({"items": [row_to_dict(r) for r in rows], "total": total, "page": page,
                        "pages": max(1, -(-total // PAGE_SIZE))})


@router.post("/{tx_id}/category")
def set_category(tx_id: int, body: ReclassBody, conn: Conn):
    return to_jsonable(reclassify(conn, tx_id, body.category_id))


TAB = Tab("transactions", "Transactions", 30, router)
