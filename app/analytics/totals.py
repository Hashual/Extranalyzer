import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.analytics.periods import Period, clip_month, month_keys
from app.importer.amounts import from_cents

UNCLASSIFIED = "À classer"

# Montant signé d'épargne : +montant sur un compte épargne, −montant ailleurs.
_SAVINGS_SIGNED = "CASE WHEN a.kind = 'savings' THEN t.amount_cents ELSE -t.amount_cents END"


@dataclass
class Totals:
    expenses: Decimal
    income: Decimal
    savings_net: Decimal
    balance: Decimal
    unclassified_debits: Decimal
    unclassified_credits: Decimal


def _scope(p: Period, account_ids: list[int] | None) -> tuple[str, list]:
    """Clause WHERE commune ; account_ids=None -> comptes courants, [] -> rien."""
    clause = "t.mirror_of IS NULL AND t.op_date BETWEEN ? AND ?"
    params: list = [p.start.isoformat(), p.end.isoformat()]
    if account_ids is None:
        clause += " AND a.kind = 'checking'"
    else:
        clause += f" AND t.account_id IN ({','.join('?' * len(account_ids))})"
        params += list(account_ids)
    return clause, params


def totals(conn: sqlite3.Connection, p: Period, account_ids: list[int] | None) -> Totals:
    if account_ids is not None and not account_ids:
        z = from_cents(0)
        return Totals(z, z, z, z, z, z)
    where, params = _scope(p, account_ids)
    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN c.kind = 'expense' THEN -t.amount_cents END), 0),
          COALESCE(SUM(CASE WHEN c.kind = 'income' THEN t.amount_cents END), 0),
          COALESCE(SUM(CASE WHEN c.kind = 'savings' THEN {_SAVINGS_SIGNED} END), 0),
          COALESCE(SUM(CASE WHEN t.category_id IS NULL AND t.amount_cents < 0
                            THEN -t.amount_cents END), 0),
          COALESCE(SUM(CASE WHEN t.category_id IS NULL AND t.amount_cents > 0
                            THEN t.amount_cents END), 0)
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE {where}
        """,
        params,
    ).fetchone()
    exp_c, inc_c, sav_c, ud_c, uc_c = row
    expenses = exp_c + ud_c
    income = inc_c + uc_c
    return Totals(
        expenses=from_cents(expenses),
        income=from_cents(income),
        savings_net=from_cents(sav_c),
        balance=from_cents(income - expenses - sav_c),
        unclassified_debits=from_cents(ud_c),
        unclassified_credits=from_cents(uc_c),
    )


def by_category(
    conn: sqlite3.Connection,
    p: Period,
    account_ids: list[int] | None,
    kind: str,
    level: Literal["parent", "child"],
    parent_id: int | None = None,
) -> list[tuple[int | None, str, Decimal]]:
    if account_ids is not None and not account_ids:
        return []
    where, params = _scope(p, account_ids)
    amount = {
        "expense": "-t.amount_cents",
        "income": "t.amount_cents",
        "savings": _SAVINGS_SIGNED,
    }[kind]
    if level == "parent":
        key, name = "COALESCE(c.parent_id, c.id)", "COALESCE(pc.name, c.name)"
    else:
        key, name = "c.id", "c.name"
    extra, extra_params = "", []
    if parent_id is not None:
        extra, extra_params = " AND (c.parent_id = ? OR c.id = ?)", [parent_id, parent_id]
    rows = conn.execute(
        f"""
        SELECT {key}, {name}, SUM({amount})
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN categories c ON c.id = t.category_id
        LEFT JOIN categories pc ON pc.id = c.parent_id
        WHERE {where} AND c.kind = ?{extra}
        GROUP BY 1, 2
        """,
        params + [kind] + extra_params,
    ).fetchall()
    result = [
        (cid, f"{n} (sans sous-catégorie)" if parent_id is not None and cid == parent_id else n,
         from_cents(cents))
        for cid, n, cents in rows
    ]
    # Opérations non classées : visibles dans la vue racine uniquement.
    if kind in ("expense", "income") and parent_id is None:
        sign = "amount_cents < 0" if kind == "expense" else "amount_cents > 0"
        cents = conn.execute(
            f"""
            SELECT COALESCE(SUM({amount}), 0)
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {where} AND t.category_id IS NULL AND t.{sign}
            """,
            params,
        ).fetchone()[0]
        if cents:
            result.append((None, UNCLASSIFIED, from_cents(cents)))
    result.sort(key=lambda r: (-r[2], r[1]))
    return result


def monthly_series(
    conn: sqlite3.Connection, p: Period, account_ids: list[int] | None
) -> list[dict]:
    series = []
    for key in month_keys(p):
        mp = clip_month(p, key)
        t = totals(conn, mp, account_ids)
        cats = {
            cid: (name, amt)
            for cid, name, amt in by_category(conn, mp, account_ids, "expense", "parent")
        }
        series.append(
            {
                "month": key,
                "expenses": t.expenses,
                "income": t.income,
                "savings_net": t.savings_net,
                "by_category": {name: amt for name, amt in cats.values()},
            }
        )
    return series


def top_increases(
    conn: sqlite3.Connection,
    cur: Period,
    ref: Period,
    account_ids: list[int] | None,
    n: int = 5,
) -> list[tuple[str, Decimal]]:
    def rows(period: Period) -> dict[int, tuple[str, Decimal]]:
        return {
            cid: (name, amt)
            for cid, name, amt in by_category(conn, period, account_ids, "expense", "parent")
            if cid is not None  # « À classer » n'est pas une catégorie comparable
        }

    now, before = rows(cur), rows(ref)
    zero = Decimal("0.00")
    diffs = [(name, amt - before.get(cid, (name, zero))[1]) for cid, (name, amt) in now.items()]
    diffs = [d for d in diffs if d[1] > 0]
    diffs.sort(key=lambda d: (-d[1], d[0]))
    return diffs[:n]
