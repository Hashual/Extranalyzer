import sqlite3

from app.analytics.periods import Period
from app.importer.amounts import from_cents

_TOLERANCE = 0.10
_MIN_MONTHS = 3


def _ordinal(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:]) - 1


def _stable(amounts: list[int]) -> bool:
    avg = sum(amounts) / len(amounts)
    return all(abs(a - avg) <= avg * _TOLERANCE for a in amounts)


def _best_window(months: list[tuple[str, int]]) -> list[tuple[str, int]] | None:
    """Plus longue fenêtre de mois consécutifs (>=3) dont les montants sont à ±10 %."""
    for size in range(len(months), _MIN_MONTHS - 1, -1):
        for i in range(len(months) - size + 1):
            window = months[i : i + size]
            if _stable([c for _, c in window]):
                return window
    return None


def recurring(
    conn: sqlite3.Connection, p: Period, account_ids: list[int] | None
) -> list[dict]:
    if account_ids is not None and not account_ids:
        return []
    sql = """
        SELECT t.label_norm, substr(t.op_date, 1, 7), -SUM(t.amount_cents)
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.mirror_of IS NULL AND t.amount_cents < 0
          AND t.op_date BETWEEN ? AND ?
          AND (c.kind IS NULL OR c.kind != 'savings')
    """
    params: list = [p.start.isoformat(), p.end.isoformat()]
    if account_ids is None:
        sql += " AND a.kind = 'checking'"
    else:
        sql += f" AND t.account_id IN ({','.join('?' * len(account_ids))})"
        params += list(account_ids)
    sql += " GROUP BY 1, 2 HAVING SUM(t.amount_cents) < 0 ORDER BY 1, 2"

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
            if w and (best is None or len(w) > len(best)):
                best = w
        if best:
            avg = round(sum(c for _, c in best) / len(best))
            found.append(
                {"label_norm": label, "avg_amount": from_cents(avg), "months": [m for m, _ in best]}
            )
    found.sort(key=lambda r: (-r["avg_amount"], r["label_norm"]))
    return found
