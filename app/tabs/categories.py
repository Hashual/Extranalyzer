"""Onglet « Catégories » : montants par catégorie racine, puis détail d'une catégorie
(sous-catégories, évolution mensuelle, transactions) avec reclassement sur place.

Les sources non classées de virements internes appariés comptent comme épargne (cf. totals) : elles
apparaissent en pseudo-ligne sans lien « Virements internes (non classés) » sous la catégorie racine
d'épargne (s'il n'y en a qu'une ; sinon en pseudo-racine), pour que l'épargne de l'onglet égale
`savings_net`.

Création, renommage et suppression des catégories : règles dans `app.classify.categories` (doublon, usages sans
destination, sous-catégories restantes : 409 ; nom ou destination invalides : 422)."""
import sqlite3
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.analytics.periods import month_keys
from app.analytics.totals import (ANALYSIS_FROM, INTERNAL_TRANSFERS, PAIRED_UNCLASSIFIED, SIGNED_AMOUNT,
                                   by_category, scope_clause, totals)
from app.classify.categories import (CategoryConflict, CategoryNotFound, create_category, delete_category,
                                     rename_category, usage)
from app.importer.amounts import from_cents
from app.deps import get_conn
from app.tabs._params import Scope, scope
from app.tabs._transactions import ROW_FROM, ROW_SELECT, ReclassBody, set_category, row_to_dict
from app.tabs.registry import Tab
from app.web_json import to_jsonable

KINDS = ("expense", "income", "savings")
DETAIL_TX_LIMIT = 100        # transactions les plus récentes ; la suite est dans l'onglet Transactions
ZERO = Decimal("0.00")

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class CategoryBody(BaseModel):
    name: str
    parent_id: int | None = None                                     # absent : nouvelle racine
    kind: Literal["expense", "income", "savings"] | None = None     # obligatoire pour une racine


class RenameBody(BaseModel):
    name: str


def _write(conn: sqlite3.Connection, fn):
    """Exécute une écriture dans une transaction ; traduit les refus en erreurs HTTP lisibles."""
    try:
        with conn:
            return fn()
    except CategoryNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except CategoryConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _category(conn: sqlite3.Connection, category_id: int) -> dict:
    cid, name, parent_id, kind = conn.execute(
        "SELECT id, name, parent_id, kind FROM categories WHERE id = ?", (category_id,)).fetchone()
    return {"id": cid, "name": name, "parent_id": parent_id, "kind": kind}


def _internal_transfers_by_month(conn: sqlite3.Connection, s: Scope) -> dict[str, int]:
    """{mois: centimes} des virements internes appariés non classés, vus comme épargne."""
    if s.account_ids is not None and not s.account_ids:
        return {}
    where, params = scope_clause(s.period, s.account_ids)
    return dict(conn.execute(
        f"SELECT substr(t.op_date, 1, 7), SUM({SIGNED_AMOUNT['savings']}) {ANALYSIS_FROM} "
        f"WHERE {where} AND {PAIRED_UNCLASSIFIED} GROUP BY 1", params).fetchall())


def _internal_transfers_host(conn: sqlite3.Connection) -> int | None:
    """Catégorie racine d'épargne qui accueille la pseudo-ligne, s'il n'y en a qu'une."""
    roots = conn.execute("SELECT id FROM categories WHERE parent_id IS NULL AND kind = 'savings'").fetchall()
    return roots[0][0] if len(roots) == 1 else None


def _unclassified_count(conn: sqlite3.Connection, s: Scope) -> int:
    """Opérations sans catégorie sur le périmètre, virements internes appariés compris."""
    if s.account_ids is not None and not s.account_ids:
        return 0
    where, params = scope_clause(s.period, s.account_ids)
    return conn.execute(f"SELECT COUNT(*) {ANALYSIS_FROM} WHERE {where} AND t.category_id IS NULL",
                        params).fetchone()[0]


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
    internal = from_cents(sum(_internal_transfers_by_month(conn, s).values()))
    if internal:
        pseudo = {"id": None, "name": INTERNAL_TRANSFERS, "amount": internal}
        host = by_id.get(_internal_transfers_host(conn))
        if host is None:
            roots.append({**pseudo, "kind": "savings", "children": []})
        else:
            host["amount"] += internal
            host["children"].append(pseudo)
    t = totals(conn, p, accounts)
    return to_jsonable({"roots": roots,
                        "unclassified": {"debits": t.unclassified_debits, "credits": t.unclassified_credits,
                                         "count": _unclassified_count(conn, s)}})


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
    if category_id == _internal_transfers_host(conn):
        internal = _internal_transfers_by_month(conn, s)
        if internal:
            children.append({"id": None, "name": INTERNAL_TRANSFERS, "amount": from_cents(sum(internal.values()))})
            children.sort(key=lambda c: (-c["amount"], c["name"]))
            per_month = {m: per_month.get(m, 0) + internal.get(m, 0) for m in per_month.keys() | internal.keys()}
    monthly = [{"month": m, "amount": from_cents(per_month.get(m, 0))} for m in month_keys(s.period)]
    transactions = conn.execute(f"{ROW_SELECT} WHERE {where} ORDER BY t.op_date DESC, t.id DESC LIMIT ?",
                                params + [DETAIL_TX_LIMIT]).fetchall()
    transactions_total = conn.execute(f"SELECT COUNT(*) {ROW_FROM} WHERE {where}", params).fetchone()[0]
    used = usage(conn, category_id)
    return to_jsonable({
        "id": category_id, "name": name, "parent_id": parent_id, "kind": kind,
        "subcategories": conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id = ?",
                                      (category_id,)).fetchone()[0],
        "usage": {"transactions": used.transactions, "rules": used.rules, "overrides": used.overrides},
        "amount": from_cents(sum(per_month.values())),
        "children": children, "monthly": monthly,
        "transactions": [row_to_dict(r) for r in transactions],
        "transactions_total": transactions_total,
    })


@router.post("", status_code=201)
def add_category(body: CategoryBody, conn: Conn):
    category_id = _write(conn, lambda: create_category(conn, body.name, body.parent_id, body.kind))
    return _category(conn, category_id)


@router.patch("/{category_id}")
def edit_category(category_id: int, body: RenameBody, conn: Conn):
    _write(conn, lambda: rename_category(conn, category_id, body.name))
    return _category(conn, category_id)


@router.delete("/{category_id}")
def remove_category(category_id: int, conn: Conn, move_to: int | None = None):
    """Supprime une catégorie ; ses usages vont vers `move_to` (obligatoire si elle est utilisée)."""
    moved = _write(conn, lambda: delete_category(conn, category_id, move_to))
    return {"moved": {"transactions": moved.transactions, "rules": moved.rules, "overrides": moved.overrides}}


@router.post("/transactions/{tx_id}/category")
def post_category(tx_id: int, body: ReclassBody, conn: Conn):
    return to_jsonable(set_category(conn, tx_id, body.category_id))


TAB = Tab("categories", "Catégories", 20, router)
