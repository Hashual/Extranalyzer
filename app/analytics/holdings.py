"""Avoirs d'épargne : livrets (relevé PDF puis opérations de l'API) et portefeuilles crypto (relevés du jour).

Montants en centimes ; une valeur inconnue vaut None, jamais 0. Aucune écriture en base."""
import sqlite3
from bisect import bisect_right
from datetime import date, timedelta
from itertools import accumulate

from app.analytics.periods import Period
from app.classify.savings_target import savings_category_for_account
from app.importer.amounts import to_cents
from app.sources.solana.holdings import SOL_MINT
from app.sources.solana.wallets import _net_deposited, hidden_mints

MAX_DAILY_POINTS = 400
ASSET_SLICES = 6            # au-delà : les plus petites parts regroupées en « Autres »
_END_OF_DAY = "T23:59:59.999999+00:00"


class _Ledger:
    """Solde d'un livret jour par jour.

    - **Relevés PDF** `ok` : leur clôture fait foi jusqu'au dernier relevé.
    - **Ancre plus récente** que le dernier relevé, la plus récente des deux :
      - le solde annoncé par la banque à la dernière synchro Enable Banking (compte rattaché, non ignoré) ;
      - le solde saisi à la main (`balance_overrides`), qui l'emporte à date égale.
      Elle corrige un trou d'historique. Les jours entre le dernier relevé et l'ancre sont remontés depuis elle
      (solde − opérations postérieures au jour).
    - **Livret non synchronisé** (aucun compte Enable Banking rattaché) : les virements du compte courant classés dans
      sa sous-catégorie d'épargne, postérieurs au dernier relevé et non appariés, comptent comme ses opérations (signe
      inversé). Les intérêts n'apparaissent qu'au relevé suivant ou à une saisie.
    """

    def __init__(self, conn: sqlite3.Connection, account_id: int):
        sections = conn.execute("SELECT statement_date, closing_cents FROM sections WHERE account_id = ? "
                                "AND status = 'ok' AND closing_cents IS NOT NULL "
                                "ORDER BY statement_date, id", (account_id,)).fetchall()
        self._section_days = [d for d, _ in sections]
        self._closings = [c for _, c in sections]
        last_statement = self._section_days[-1] if self._section_days else None
        ops = conn.execute("SELECT op_date, amount_cents FROM transactions WHERE account_id = ?",
                           (account_id,)).fetchall()
        synced = conn.execute("SELECT 1 FROM external_accounts WHERE account_id = ? AND ignored = 0 LIMIT 1",
                              (account_id,)).fetchone() is not None
        self._inferred_days: list[str] = []
        if not synced:
            category = savings_category_for_account(conn, account_id, create=False)
            if category is not None:
                inferred = conn.execute(
                    "SELECT t.op_date, -t.amount_cents FROM transactions t JOIN accounts a ON a.id = t.account_id "
                    "WHERE t.category_id = ? AND a.kind = 'checking' "
                    "AND t.account_id != ? AND t.mirror_of IS NULL AND t.op_date > ? "
                    "AND NOT EXISTS (SELECT 1 FROM transactions m WHERE m.mirror_of = t.id)",
                    (category, account_id, last_statement or "")).fetchall()
                ops += inferred
                self._inferred_days = sorted(d for d, _ in inferred)
        ops.sort(key=lambda o: o[0])
        self._op_days = [d for d, _ in ops]
        self._cumulative = [0, *accumulate(a for _, a in ops)]
        bank = conn.execute("SELECT balance_date, balance_cents FROM external_accounts WHERE account_id = ? "
                            "AND ignored = 0 AND balance_cents IS NOT NULL AND balance_date IS NOT NULL "
                            "ORDER BY balance_date DESC, id DESC LIMIT 1", (account_id,)).fetchone()
        manual = conn.execute("SELECT day, cents FROM balance_overrides WHERE account_id = ?",
                              (account_id,)).fetchone()
        anchors = []
        if bank is not None and (last_statement is None or bank[0] > last_statement):
            anchors.append((bank[0], 0, bank[1], "bank"))
        if manual is not None and (last_statement is None or manual[0] >= last_statement):
            anchors.append((manual[0], 1, manual[1], "manual"))        # à date égale, la saisie l'emporte
        best = max(anchors) if anchors else None
        self._anchor = None if best is None else (best[0], best[2], best[3])

    def _ops_through(self, day_iso: str) -> int:
        return self._cumulative[bisect_right(self._op_days, day_iso)]

    def _uses_anchor(self, iso: str, i: int) -> bool:
        if self._anchor is None or i != len(self._section_days) - 1:
            return False
        return i < 0 or iso > self._section_days[i] or (iso == self._section_days[i] and self._anchor[0] == iso)

    def value(self, day: date) -> int | None:
        iso = day.isoformat()
        i = bisect_right(self._section_days, iso) - 1
        if self._uses_anchor(iso, i):
            anchor_day, anchor_cents, _ = self._anchor
            if i < 0 and iso < min(anchor_day, self._op_days[0] if self._op_days else anchor_day):
                return None          # sans relevé PDF : rien de connu avant la première opération ou l'ancre
            return anchor_cents + self._ops_through(iso) - self._ops_through(anchor_day)
        if i < 0:
            return None
        return self._closings[i] + self._ops_through(iso) - self._ops_through(self._section_days[i])

    def source(self, day: date) -> str | None:
        """Origine du solde au jour : 'bank', 'manual', 'estimated' (relevé + virements déduits) ou 'statement'."""
        iso = day.isoformat()
        i = bisect_right(self._section_days, iso) - 1
        if self._uses_anchor(iso, i):
            return self._anchor[2] if self.value(day) is not None else None
        if i < 0:
            return None
        start = self._section_days[i]
        j = bisect_right(self._inferred_days, start)
        return "estimated" if j < len(self._inferred_days) and self._inferred_days[j] <= iso else "statement"

    def as_of(self, day: date) -> date | None:
        """Date du solde de référence utilisé au jour (relevé, synchro ou saisie)."""
        iso = day.isoformat()
        i = bisect_right(self._section_days, iso) - 1
        if self._uses_anchor(iso, i):
            return date.fromisoformat(self._anchor[0])
        return None if i < 0 else date.fromisoformat(self._section_days[i])


def set_manual_balance(conn: sqlite3.Connection, account_id: int, day: date, cents: int, now: str) -> None:
    """Solde d'un livret saisi à la main (remplace la saisie précédente). Ne valide pas."""
    conn.execute("INSERT INTO balance_overrides(account_id, day, cents, created_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(account_id) DO UPDATE SET day = excluded.day, cents = excluded.cents, "
                 "created_at = excluded.created_at", (account_id, day.isoformat(), cents, now))


def clear_manual_balance(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM balance_overrides WHERE account_id = ?", (account_id,))


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


def _is_sol(mint: str | None) -> bool:
    """Position de staking en SOL : compte Stake Solana (sans mint) ou SOL placé sur Kraken (mint du SOL)."""
    return mint is None or mint == SOL_MINT


def _short(mint: str) -> str:
    return mint if len(mint) <= 9 else f"{mint[:4]}…{mint[-4:]}"


def _group_slices(parts: dict[str, int]) -> list[dict]:
    ranked = sorted(parts.items(), key=lambda kv: -kv[1])
    kept = [{"name": n, "value": v} for n, v in ranked[:ASSET_SLICES]]
    if len(ranked) > ASSET_SLICES:
        kept.append({"name": "Autres", "value": sum(v for _, v in ranked[ASSET_SLICES:])})
    return kept


SOL_KEY = "SOL"             # SOL disponible et SOL en staking : un seul actif dans la répartition des cryptos
ASSET_KEY_MAX = 64
GROUP_NAME_MAX = 40
GROUP_PREFIX = "group:"     # clé d'un groupe dans crypto_categories


def _crypto_assets(conn: sqlite3.Connection, snapshot_ids: list[int], hidden: frozenset[str]) -> dict[str, dict]:
    """Actifs des relevés retenus, additionnés tous portefeuilles confondus : clé → {name, value}.

    Clé : SOL_KEY pour le SOL (disponible ou en staking), l'adresse du jeton (mint) sinon ; pour Kraken, l'adresse
    Solana de la même crypto quand elle est connue, sinon son symbole (voir app.sources.kraken.holdings)."""
    assets: dict[str, dict] = {}
    for snapshot_id in snapshot_ids:
        for kind, mint, symbol, cents in conn.execute(
                "SELECT kind, mint, symbol, value_eur_cents FROM wallet_positions WHERE snapshot_id = ?",
                (snapshot_id,)):
            if cents is None or (kind == "token" and mint in hidden):
                continue
            if kind == "sol" or (kind == "stake" and _is_sol(mint)):
                key, name = SOL_KEY, "SOL"
            else:
                key = mint or "?"
                name = symbol or (_short(mint) if mint else "Jeton inconnu")
            entry = assets.setdefault(key, {"name": name, "value": 0})
            entry["value"] += cents
    return assets


def crypto_categories(conn: sqlite3.Connection, snapshot_ids: list[int], hidden: frozenset[str]) -> list[dict]:
    """Répartition des cryptos par catégorie : un actif seul, ou un groupe que l'utilisateur a formé (« Altcoins »,
    ou une fusion comme WBTC + BTC sous « BTC »). La clé d'un groupe est GROUP_PREFIX + son nom : elle ne se
    confond jamais avec un actif nommé pareil (le BTC de Kraken a pour clé « BTC »)."""
    assets = _crypto_assets(conn, snapshot_ids, hidden)
    groups = dict(conn.execute("SELECT asset_key, group_name FROM crypto_groups"))
    categories: dict[str, dict] = {}
    for key, asset in assets.items():
        group = groups.get(key)
        if group is None:
            categories[key] = {"key": key, "name": asset["name"], "group": False, "value": asset["value"],
                               "members": []}
            continue
        gkey = GROUP_PREFIX + group
        entry = categories.setdefault(gkey, {"key": gkey, "name": group, "group": True, "value": 0, "members": []})
        entry["value"] += asset["value"]
        entry["members"].append({"key": key, "name": asset["name"], "value": asset["value"]})
    result = []
    for entry in categories.values():
        entry["members"].sort(key=lambda m: (-m["value"], m["name"]))
        result.append(entry)
    result.sort(key=lambda c: (-c["value"], c["name"]))
    return result


def set_crypto_group(conn: sqlite3.Connection, asset_key: str, group: str | None) -> None:
    """Range un actif dans un groupe, ou l'en retire (group=None) : un groupe réduit à un seul membre est alors
    dissous (« Retirer » WBTC de « BTC (2) » rend BTC seul). Ne valide pas."""
    if group is None:
        row = conn.execute("SELECT group_name FROM crypto_groups WHERE asset_key = ?", (asset_key,)).fetchone()
        conn.execute("DELETE FROM crypto_groups WHERE asset_key = ?", (asset_key,))
        if row is not None:
            conn.execute("DELETE FROM crypto_groups WHERE group_name = ? AND "
                         "(SELECT COUNT(*) FROM crypto_groups WHERE group_name = ?) = 1", (row[0], row[0]))
    else:
        conn.execute("INSERT INTO crypto_groups(asset_key, group_name) VALUES (?, ?) "
                     "ON CONFLICT(asset_key) DO UPDATE SET group_name = excluded.group_name", (asset_key, group))


def merge_crypto(conn: sqlite3.Connection, source: str, target: str, name: str) -> str:
    """Fusionne la catégorie `source` dans `target` (clés de crypto_categories : actif ou GROUP_PREFIX + groupe).

    Le groupe d'arrivée est celui de la cible : le groupe lui-même, celui où l'actif est déjà rangé, sinon un groupe
    nommé `name` où la cible entre aussi. Une source groupe y verse tous ses membres. Renvoie le nom du groupe.
    Ne valide pas ; ValueError si source et cible sont la même catégorie."""
    if source == target:
        raise ValueError("une crypto ne peut pas fusionner avec elle-même")
    if target.startswith(GROUP_PREFIX):
        group = target[len(GROUP_PREFIX):]
    else:
        row = conn.execute("SELECT group_name FROM crypto_groups WHERE asset_key = ?", (target,)).fetchone()
        group = row[0] if row is not None else name
        set_crypto_group(conn, target, group)
    if source.startswith(GROUP_PREFIX):
        conn.execute("UPDATE crypto_groups SET group_name = ? WHERE group_name = ?",
                     (group, source[len(GROUP_PREFIX):]))
    else:
        set_crypto_group(conn, source, group)
    return group


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
                       "deposited": None, "gap": None, "unknown": value is None,
                       "balance_source": ledger.source(as_of), "balance_as_of": ledger.as_of(as_of)})
    for wallet_id, label, category_id, provider in conn.execute(
            "SELECT id, label, savings_category_id, provider FROM wallets ORDER BY id"):
        snaps = _Snapshots(conn, wallet_id)
        found = snaps.visible_at(conn, as_of, hidden)
        value = None if found is None else found[0]
        past = snaps.visible_at(conn, before, hidden)
        deposited = None if category_id is None else to_cents(_net_deposited(conn, category_id)[2])
        if found is not None:
            retained.append(found)
        place = {"kind": "wallet", "provider": provider, "id": wallet_id, "name": label, "category_id": category_id,
                 "value": value,
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

    categories = crypto_categories(conn, [snapshot_id for _, snapshot_id, _ in retained], hidden)
    by_asset: dict[str, int] = {}
    if savings_value is not None and savings_value > 0:
        by_asset["Livrets"] = savings_value
    for category in categories:                 # mêmes parts que la répartition des cryptos (fusions comprises)
        by_asset[category["name"]] = by_asset.get(category["name"], 0) + category["value"]

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
        "crypto_categories": categories,
    }
