import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.analytics.periods import Period, clip_month, month_keys
from app.importer.amounts import from_cents

UNCLASSIFIED = "À classer"
INTERNAL_TRANSFERS = "Virements internes (non classés)"

# Montant (en centimes) d'une opération vu depuis une catégorie de ce `kind`, sur
# `transactions t JOIN accounts a`. Épargne : +montant sur un compte épargne, −montant ailleurs.
SIGNED_AMOUNT = {
    "expense": "-t.amount_cents",
    "income": "t.amount_cents",
    "savings": "CASE WHEN a.kind = 'savings' THEN t.amount_cents ELSE -t.amount_cents END",
}

# Source non classée d'un virement interne apparié (une jambe miroir la désigne) : c'est un flux
# d'épargne, pas une dépense ni un revenu, même tant que l'utilisateur n'a pas choisi sa destination.
PAIRED_UNCLASSIFIED = ("(t.category_id IS NULL AND t.mirror_of IS NULL AND EXISTS "
                       "(SELECT 1 FROM transactions m WHERE m.mirror_of = t.id))")

# `kind` effectif d'une opération, sur `transactions t LEFT JOIN categories c` : celui de sa catégorie,
# 'savings' pour un virement interne apparié non classé, NULL pour les autres opérations à classer.
# Toute analyse par nature (totaux, répartition, épargne, récurrents) passe par lui.
EFFECTIVE_KIND = f"CASE WHEN c.kind IS NOT NULL THEN c.kind WHEN {PAIRED_UNCLASSIFIED} THEN 'savings' END"

ANALYSIS_FROM = """
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN categories c ON c.id = t.category_id
"""


@dataclass
class Totals:
    expenses: Decimal
    income: Decimal
    savings_net: Decimal
    balance: Decimal
    unclassified_debits: Decimal
    unclassified_credits: Decimal


def unclassified_count(conn: sqlite3.Connection) -> int:
    """Opérations à classer, tous comptes et toutes dates (jambes miroir exclues : elles suivent leur
    source). Seule définition, partagée par la pastille de la barre, l'onglet À classer et les scripts."""
    return conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND mirror_of IS NULL").fetchone()[0]


def scope_clause(p: Period, account_ids: list[int] | None, include_mirrors: bool = False) -> tuple[str, list]:
    """Clause WHERE commune sur `transactions t JOIN accounts a` : période, comptes
    (None -> comptes courants, [] -> rien) et, sauf `include_mirrors`, sans jambes miroir."""
    clause = "t.op_date BETWEEN ? AND ?"
    params: list = [p.start.isoformat(), p.end.isoformat()]
    if not include_mirrors:
        clause += " AND t.mirror_of IS NULL"
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
    where, params = scope_clause(p, account_ids)
    ek = f"({EFFECTIVE_KIND})"
    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN {ek} = 'expense' THEN {SIGNED_AMOUNT['expense']} END), 0),
          COALESCE(SUM(CASE WHEN {ek} = 'income' THEN {SIGNED_AMOUNT['income']} END), 0),
          COALESCE(SUM(CASE WHEN {ek} = 'savings' THEN {SIGNED_AMOUNT['savings']} END), 0),
          COALESCE(SUM(CASE WHEN {ek} IS NULL AND t.amount_cents < 0 THEN -t.amount_cents END), 0),
          COALESCE(SUM(CASE WHEN {ek} IS NULL AND t.amount_cents > 0 THEN t.amount_cents END), 0)
        {ANALYSIS_FROM}
        WHERE {where}
        """,
        params,
    ).fetchone()
    exp_c, inc_c, sav_c, ud_c, uc_c = row
    expenses = exp_c + ud_c
    income = inc_c  # les crédits non classés restent à part (unclassified_credits)
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
    where, params = scope_clause(p, account_ids)
    amount = SIGNED_AMOUNT[kind]
    pseudo = {"expense": (UNCLASSIFIED, "IS NULL AND t.amount_cents < 0"),
              "savings": (INTERNAL_TRANSFERS, "= 'savings'")}
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
    # Opérations sans catégorie de ce `kind` effectif : une ligne sans id, dans la vue racine uniquement.
    # Pas de ligne pour les revenus : un crédit non classé n'est pas un revenu.
    if parent_id is None and kind in pseudo:
        label, condition = pseudo[kind]
        cents = conn.execute(
            f"""
            SELECT COALESCE(SUM({amount}), 0) {ANALYSIS_FROM}
            WHERE {where} AND t.category_id IS NULL AND ({EFFECTIVE_KIND}) {condition}
            """,
            params,
        ).fetchone()[0]
        if cents:
            result.append((None, label, from_cents(cents)))
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
