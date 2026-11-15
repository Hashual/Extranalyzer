"""Avoirs d'épargne : livrets (relevé PDF puis opérations de l'API) et portefeuilles crypto (relevés du jour).

Montants en centimes ; une valeur inconnue vaut None, jamais 0. Aucune écriture en base."""
import sqlite3
from bisect import bisect_right
from datetime import date, timedelta
from itertools import accumulate

from app.analytics.periods import Period
from app.classify.savings_target import savings_category_for_account
from app.importer.amounts import to_cents
from app.sources.solana.wallets import _net_deposited, hidden_mints

MAX_DAILY_POINTS = 400
ASSET_SLICES = 6            # au-delà : les plus petites parts regroupées en « Autres »
_END_OF_DAY = "T23:59:59.999999+00:00"


class _Ledger:
    """Solde d'un livret jour par jour : dernier relevé `ok` ≤ jour, plus les opérations postérieures à ce relevé."""

    def __init__(self, conn: sqlite3.Connection, account_id: int):
        sections = conn.execute("SELECT statement_date, closing_cents FROM sections WHERE account_id = ? "
                                "AND status = 'ok' AND closing_cents IS NOT NULL "
                                "ORDER BY statement_date, id", (account_id,)).fetchall()
        self._section_days = [d for d, _ in sections]
        self._closings = [c for _, c in sections]
        ops = conn.execute("SELECT op_date, amount_cents FROM transactions WHERE account_id = ? "
                           "ORDER BY op_date", (account_id,)).fetchall()
        self._op_days = [d for d, _ in ops]
        self._cumulative = [0, *accumulate(a for _, a in ops)]

    def _ops_through(self, day_iso: str) -> int:
        return self._cumulative[bisect_right(self._op_days, day_iso)]

    def value(self, day: date) -> int | None:
        i = bisect_right(self._section_days, day.isoformat()) - 1
        if i < 0:
            return None
        return self._closings[i] + self._ops_through(day.isoformat()) - self._ops_through(self._section_days[i])


class _Snapshots:
    """Valeur d'un portefeuille jour par jour : dernier relevé `ok` pris au plus tard à la fin du jour (UTC)."""

    def __init__(self, conn: sqlite3.Connection, wallet_id: int):
        rows = conn.execute("SELECT taken_at, total_eur_cents, id, unpriced_count FROM wallet_snapshots "
                            "WHERE wallet_id = ? AND status = 'ok' AND total_eur_cents IS NOT NULL "
                            "ORDER BY taken_at, id", (wallet_id,)).fetchall()
        self._taken = [r[0] for r in rows]
        self._rows = rows

    def at(self, day: date) -> tuple[int, int, int] | None:
        """(valeur, id du relevé, positions sans prix), ou None sans relevé."""
        i = bisect_right(self._taken, day.isoformat() + _END_OF_DAY) - 1
        return None if i < 0 else (self._rows[i][1], self._rows[i][2], self._rows[i][3])

    def value(self, day: date) -> int | None:
        found = self.at(day)
        return None if found is None else found[0]

    def visible_at(self, conn: sqlite3.Connection, day: date,
                   hidden: frozenset[str]) -> tuple[int, int, int] | None:
        """Comme `at`, recalculé depuis les positions du relevé hors jetons masqués aujourd'hui (même règle que
        wallets._positions). Sans aucune position enregistrée, on garde le total stocké."""
        found = self.at(day)
        if found is None:
            return None
        rows = conn.execute("SELECT kind, mint, value_eur_cents FROM wallet_positions WHERE snapshot_id = ?",
                            (found[1],)).fetchall()
        if not rows:
            return found
        visible = [cents for kind, mint, cents in rows if not (kind == "token" and mint in hidden)]
        return sum(c for c in visible if c is not None), found[1], sum(1 for c in visible if c is None)

def savings_balance(conn: sqlite3.Connection, account_id: int, day: date) -> int | None:
    return _Ledger(conn, account_id).value(day)


def wallet_value(conn: sqlite3.Connection, wallet_id: int, day: date) -> int | None:
    return _Snapshots(conn, wallet_id).value(day)


def crypto_tracked_since(conn: sqlite3.Connection) -> date | None:
    """Jour (UTC) du premier relevé crypto `ok` : avant, l'absence de valeur ne signifie pas zéro."""
    first = conn.execute("SELECT MIN(taken_at) FROM wallet_snapshots WHERE status = 'ok' "
                         "AND total_eur_cents IS NOT NULL").fetchone()[0]
    return None if first is None else date.fromisoformat(first[:10])


def _sum_known(values: list[int | None]) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _sample_days(start: date, end: date) -> list[date]:
    n = (end - start).days + 1
    if n <= 0:
        return []
    if n <= MAX_DAILY_POINTS:
        return [start + timedelta(days=i) for i in range(n)]
    days = []
    day = end
    while day >= start:                     # un point par semaine, le dernier tombe sur la fin
        days.append(day)
        day -= timedelta(days=7)
    return days[::-1]


def series(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    ledgers = [_Ledger(conn, i) for (i,) in conn.execute("SELECT id FROM accounts WHERE kind = 'savings' ORDER BY id")]
    snapshots = [_Snapshots(conn, i) for (i,) in conn.execute("SELECT id FROM wallets ORDER BY id")]
    since = crypto_tracked_since(conn)
    points = []
    for day in _sample_days(start, end):
        crypto = None if since is None or day < since else _sum_known([s.value(day) for s in snapshots])
        points.append({"date": day, "savings": _sum_known([ledger.value(day) for ledger in ledgers]),
                       "crypto": crypto})
    return points


def _short(mint: str) -> str:
    return mint if len(mint) <= 9 else f"{mint[:4]}…{mint[-4:]}"


def _wallet_assets(conn: sqlite3.Connection, snapshot_id: int, hidden: frozenset[str]) -> dict[str, int]:
    parts: dict[str, int] = {}
    for kind, mint, symbol, cents in conn.execute(
            "SELECT kind, mint, symbol, value_eur_cents FROM wallet_positions WHERE snapshot_id = ?", (snapshot_id,)):
        if cents is None or (kind == "token" and mint in hidden):
            continue
        if kind == "stake":
            name = "SOL en staking"
        elif kind == "sol":
            name = "SOL disponible"
        else:
            name = symbol or (_short(mint) if mint else "Jeton inconnu")
        parts[name] = parts.get(name, 0) + cents
    return parts


def _group_slices(parts: dict[str, int]) -> list[dict]:
    ranked = sorted(parts.items(), key=lambda kv: -kv[1])
    kept = [{"name": n, "value": v} for n, v in ranked[:ASSET_SLICES]]
    if len(ranked) > ASSET_SLICES:
        kept.append({"name": "Autres", "value": sum(v for _, v in ranked[ASSET_SLICES:])})
    return kept


def _change(end: int | None, before: int | None) -> int | None:
    return None if end is None or before is None else end - before


def dashboard(conn: sqlite3.Connection, period: Period, today: date) -> dict:
    as_of = min(period.end, today)
    before = period.start - timedelta(days=1)
    hidden = hidden_mints(conn)
    places, savings_values, wallet_places, retained = [], [], [], []
    for account_id, label in conn.execute("SELECT id, label FROM accounts WHERE kind = 'savings' ORDER BY id"):
        ledger = _Ledger(conn, account_id)
        value = ledger.value(as_of)
        savings_values.append(value)
        places.append({"kind": "savings", "id": account_id, "name": label,
                       "category_id": savings_category_for_account(conn, account_id, create=False),
                       "value": value, "change": _change(value, ledger.value(before)),
                       "deposited": None, "gap": None, "unknown": value is None})
    for wallet_id, label, category_id in conn.execute("SELECT id, label, savings_category_id FROM wallets ORDER BY id"):
        snaps = _Snapshots(conn, wallet_id)
        found = snaps.visible_at(conn, as_of, hidden)
        value = None if found is None else found[0]
        past = snaps.visible_at(conn, before, hidden)
        deposited = None if category_id is None else to_cents(_net_deposited(conn, category_id)[2])
        if found is not None:
            retained.append(found)
        place = {"kind": "wallet", "id": wallet_id, "name": label, "category_id": category_id, "value": value,
                 "change": _change(value, None if past is None else past[0]), "deposited": deposited,
                 "gap": None if value is None or deposited is None else value - deposited, "unknown": value is None}
        wallet_places.append(place)
        places.append(place)

    savings_value = _sum_known(savings_values)
    wallet_values = [p["value"] for p in wallet_places]
    crypto_value = _sum_known(wallet_values)
    comparable = [p for p in wallet_places if p["value"] is not None and p["deposited"] is not None]
    crypto_deposited = _sum_known([p["deposited"] for p in comparable])       # même ensemble que compared_value
    compared_value = _sum_known([p["value"] for p in comparable])
    compared_partial = len(comparable) < sum(1 for v in wallet_values if v is not None)

    places.sort(key=lambda p: (p["value"] is None, -(p["value"] or 0)))
    known = [p for p in places if p["value"] is not None]
    changes = [p["change"] for p in known if p["change"] is not None]
    scope = None if not changes else ("all" if len(changes) == len(known) else "partial")

    by_asset: dict[str, int] = {}
    if savings_value is not None and savings_value > 0:
        by_asset["Livrets"] = savings_value
    for _, snapshot_id, _ in retained:
        for name, cents in _wallet_assets(conn, snapshot_id, hidden).items():
            by_asset[name] = by_asset.get(name, 0) + cents

    return {
        "as_of": as_of,
        "total": {"value": _sum_known([savings_value, crypto_value]), "change": sum(changes) if changes else None,
                  "change_scope": scope},
        "savings": {"value": savings_value, "count": len(savings_values),
                    "unknown_count": sum(1 for v in savings_values if v is None)},
        "crypto": {"value": crypto_value, "count": len(wallet_places),
                   "unread_count": sum(1 for v in wallet_values if v is None),
                   "unpriced_count": sum(unpriced for _, _, unpriced in retained), "deposited": crypto_deposited,
                   "compared_value": compared_value, "compared_partial": compared_partial,
                   "gap": None if compared_value is None or crypto_deposited is None
                   else compared_value - crypto_deposited,
                   "tracked_since": crypto_tracked_since(conn)},
        "places": places,
        "allocation": {"by_place": [{"name": p["name"], "value": p["value"]} for p in known if p["value"] > 0],
                       "by_asset": _group_slices(by_asset)},
        "series": series(conn, period.start, as_of),
    }
