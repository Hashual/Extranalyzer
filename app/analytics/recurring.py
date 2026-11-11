import sqlite3

from app.analytics.periods import Period
from app.analytics.totals import ANALYSIS_FROM, EFFECTIVE_KIND, scope_clause
from app.importer.amounts import from_cents

_MIN_MONTHS = 3


def _ordinal(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:]) - 1


def _stable(amounts: list[int]) -> bool:
    n, total = len(amounts), sum(amounts)
    # |a - total/n| <= 10 % de total/n, en entiers : 10 * |a*n - total| <= total
    return all(10 * abs(a * n - total) <= total for a in amounts)


def _best_window(months: list[tuple[str, int]]) -> list[tuple[str, int]] | None:
    """Plus longue fenêtre (>=3 mois consécutifs) stable à ±10 % ; à taille égale, la plus récente."""
    for size in range(len(months), _MIN_MONTHS - 1, -1):
        for i in range(len(months) - size, -1, -1):
            window = months[i : i + size]
            if _stable([c for _, c in window]):
                return window
    return None


def recurring(
    conn: sqlite3.Connection, p: Period, account_ids: list[int] | None
) -> list[dict]:
    if account_ids is not None and not account_ids:
        return []
    where, params = scope_clause(p, account_ids)
    sql = f"""
        SELECT t.label_norm, substr(t.op_date, 1, 7), -SUM(t.amount_cents)
        {ANALYSIS_FROM}
        WHERE {where} AND t.amount_cents < 0
          AND ({EFFECTIVE_KIND}) IS NOT 'savings'
    """
    sql += " GROUP BY 1, 2 ORDER BY 1, 2"

    by_label: dict[str, list[tuple[str, int]]] = {}
    for label, month, cents in conn.execute(sql, params):
        by_label.setdefault(label, []).append((month, cents))

    found = []
    for label, months in by_label.items():
        # Découpe en séquences de mois calendaires consécutifs.
        runs, run = [], [months[0]]
        for m in months[1:]:
            if _ordinal(m[0]) == _ordinal(run[-1][0]) + 1:
                run.append(m)
            else:
                runs.append(run)
                run = [m]
        runs.append(run)
        best = None
        for r in runs:
            w = _best_window(r) if len(r) >= _MIN_MONTHS else None
            if w and (best is None or len(w) >= len(best)):
                best = w
        if best:
            total, k = sum(c for _, c in best), len(best)
            avg = (2 * total + k) // (2 * k)  # arrondi au centime, sans flottant
            found.append(
                {"label_norm": label, "avg_amount": from_cents(avg), "months": [m for m, _ in best]}
            )
    found.sort(key=lambda r: (-r["avg_amount"], r["label_norm"]))
    return found
