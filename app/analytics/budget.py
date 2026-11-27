"""Budget : objectifs mensuels datés par catégorie racine de dépense, suivi, assistant et conseils.

Objectifs : table `budget_targets` ; l'objectif d'un mois M est la ligne la plus récente dont `effective_from`
(« AAAA-MM ») précède ou vaut M (NULL : plus d'objectif à partir de ce mois).

Habitudes : les mois complets (au plus `HISTORY_MONTHS`) avant le mois de `today`, limités aux mois couverts
par les données. Elles servent à répartir un objectif dans le mois (le loyer part le 1er : l'« attendu à ce
jour » le sait), à projeter la fin de période et à proposer des objectifs.

Tout est calculé en centimes ; les montants de dépense sont positifs (remboursements déduits).
"""
import calendar
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.analytics.periods import Period, clip_month, month_keys
from app.analytics.recurring import recurring
from app.analytics.totals import ANALYSIS_FROM, EFFECTIVE_KIND, UNCLASSIFIED, scope_clause, totals
from app.importer.amounts import from_cents
from app.store.repo import fold

HISTORY_MONTHS = 6
RISK_MARGIN = 1.10              # dépensé > attendu × 1,1 : à surveiller
RISK_MIN_CENTS = 500            # … et d'au moins 5 €, pour ne pas alerter sur des centimes
SMALL_PURCHASE_CENTS = 1500     # « petits achats » : moins de 15 €
EFFORTS = {"light": 0.05, "medium": 0.10, "strong": 0.20}
VARIABLE_FLOOR = 0.5            # l'assistant ne baisse jamais un poste variable sous la moitié de sa moyenne
ROUND_TO = 500                  # propositions arrondies aux 5 € supérieurs
FIXED_CV = 0.20                 # poste fixe : coefficient de variation mensuel <= 20 %…
FIXED_PRESENCE = 0.8            # … et présent au moins 80 % des mois

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MERCHANT_PREFIX = re.compile(r"^(PAIEMENT (CB|PSC|MOB)|PRLV SEPA|CB)\s+")

# Astuce par catégorie (nom sans accents, en majuscules, préfixe), proposée quand la catégorie pèse lourd.
CATEGORY_TIPS = {
    "COURSES": "faire une liste et passer par le drive limite les achats d'impulsion ; les marques distributeur "
               "coûtent souvent 20 à 30 % moins cher.",
    "RESTAURATION": "préparer ses déjeuners quelques jours par semaine fait vite baisser ce poste ; se fixer un "
                    "nombre de restos par mois est plus parlant qu'un montant.",
    "TRANSPORT": "covoiturage, abonnement de transport ou comparateur de prix du carburant selon tes trajets.",
    "SHOPPING": "la règle des 48 h (attendre deux jours avant un achat non prévu) élimine une bonne partie des "
                "achats impulsifs.",
    "LOISIRS": "regarde les cartes d'abonnement et tarifs réduits avant de payer plein tarif.",
    "FACTURES": "compare chaque année box, mobile et énergie : changer d'offre prend souvent dix minutes.",
}


def is_month(text: str) -> bool:
    return bool(_MONTH.fullmatch(text))


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _shift(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _days_in(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:]))[1]


def _month_period(month: str) -> Period:
    y, m = int(month[:4]), int(month[5:])
    return Period(date(y, m, 1), date(y, m, _days_in(month)))


# --- Données ------------------------------------------------------------------------------------

def budget_categories(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Catégories budgétables : les racines de dépense, par nom."""
    return conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL AND kind = 'expense' "
                        "ORDER BY name").fetchall()


class Targets:
    """Objectifs datés : `get(cat, month)` -> centimes ou None."""

    def __init__(self, conn: sqlite3.Connection):
        self.rows: dict[int, list[tuple[str, int | None]]] = defaultdict(list)
        for cid, since, cents in conn.execute(
                "SELECT category_id, effective_from, monthly_cents FROM budget_targets ORDER BY effective_from"):
            self.rows[cid].append((since, cents))

    def get(self, category_id: int, month: str) -> int | None:
        value = None
        for since, cents in self.rows.get(category_id, ()):
            if since > month:
                break
            value = cents
        return value

    def since(self, category_id: int, month: str) -> str | None:
        """Mois depuis lequel l'objectif de `month` s'applique."""
        found = None
        for since, _ in self.rows.get(category_id, ()):
            if since > month:
                break
            found = since
        return found


class Spend:
    """Dépenses par catégorie racine (None : débits à classer) et par jour, sur [start, end]."""

    def __init__(self, conn: sqlite3.Connection, start: date, end: date, account_ids: list[int] | None):
        self.days: dict[tuple[int | None, str], list[int]] = {}
        self.first: str | None = None
        self.last: str | None = None
        if account_ids is not None and not account_ids:
            return
        where, params = scope_clause(Period(start, end), account_ids)
        rows = conn.execute(
            f"""SELECT CASE WHEN c.kind = 'expense' THEN COALESCE(c.parent_id, c.id) END, t.op_date,
                       SUM(-t.amount_cents)
                {ANALYSIS_FROM}
                WHERE {where} AND (c.kind = 'expense'
                      OR (t.category_id IS NULL AND ({EFFECTIVE_KIND}) IS NULL AND t.amount_cents < 0))
                GROUP BY 1, 2""", params).fetchall()
        for cat, day, cents in rows:
            self.days.setdefault((cat, day[:7]), [0] * 32)[int(day[8:10])] += cents
        everything = Period(date(1, 1, 1), date(9999, 12, 31))
        where, params = scope_clause(everything, account_ids)
        self.first, self.last = conn.execute(
            f"SELECT MIN(t.op_date), MAX(t.op_date) {ANALYSIS_FROM} WHERE {where}", params).fetchone()

    def window(self, cat: int | None, month: str, d1: int, d2: int) -> int:
        days = self.days.get((cat, month))
        return 0 if days is None or d1 > d2 else sum(days[d1:d2 + 1])

    def month(self, cat: int | None, month: str) -> int:
        days = self.days.get((cat, month))
        return 0 if days is None else sum(days)

    def between(self, cat: int | None, p: Period) -> int:
        total = 0
        for mk in month_keys(p):
            mp = clip_month(p, mk)
            total += self.window(cat, mk, mp.start.day, mp.end.day)
        return total


def history_months(spend: Spend, today: date) -> list[str]:
    """Mois complets avant celui de `today` (au plus HISTORY_MONTHS), couverts par les données."""
    if spend.first is None:
        return []
    current = month_of(today)
    first, last = spend.first[:7], spend.last[:7]
    months = [_shift(current, -i) for i in range(HISTORY_MONTHS, 0, -1)]
    return [m for m in months if first <= m <= last]


def _load(conn, start: date, end: date, account_ids, today: date) -> tuple[Spend, list[str]]:
    """Charge les dépenses de [start, end] et des mois d'habitude ; renvoie aussi ces mois."""
    hist_start = _month_period(_shift(month_of(today), -HISTORY_MONTHS)).start
    spend = Spend(conn, min(start, hist_start), max(end, today), account_ids)
    return spend, history_months(spend, today)


class Habits:
    """Rythme habituel d'une catégorie dans le mois, d'après les mois d'habitude."""

    def __init__(self, spend: Spend, months: list[str]):
        self.spend, self.months = spend, months

    def _window(self, cat, month: str, target_month: str, d1: int, d2: int) -> int:
        dim, target_dim = _days_in(month), _days_in(target_month)
        hi = dim if d2 >= target_dim else min(d2, dim)          # « jusqu'à la fin du mois » reste la fin du mois
        return self.spend.window(cat, month, d1, hi) if d1 <= dim else 0

    def share(self, cat, target_month: str, d1: int, d2: int) -> float:
        """Part des dépenses du mois faite d'habitude du jour d1 au jour d2 (prorata des jours à défaut)."""
        dim = _days_in(target_month)
        if d1 <= 1 and d2 >= dim:
            return 1.0
        if d1 > d2:
            return 0.0
        total = sum(self.spend.month(cat, m) for m in self.months)
        if total > 0:
            part = sum(self._window(cat, m, target_month, d1, d2) for m in self.months)
            return min(max(part / total, 0.0), 1.0)
        return (d2 - d1 + 1) / dim

    def typical(self, cat, target_month: str, d1: int, d2: int) -> float | None:
        """Dépense moyenne de la catégorie du jour d1 au jour d2, None sans habitudes."""
        if not self.months:
            return None
        if d1 > d2:
            return 0.0
        return sum(self._window(cat, m, target_month, d1, d2) for m in self.months) / len(self.months)


# --- Suivi --------------------------------------------------------------------------------------

def _status(target: int | None, spent: int, expected: float | None, projection: float | None) -> str:
    if target is None:
        return "none"
    if spent > target:
        return "over"
    if projection is not None and projection > target:
        return "risk"
    if expected is not None and spent > expected * RISK_MARGIN and spent - expected >= RISK_MIN_CENTS:
        return "risk"
    return "ok"


def _cents(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def summary(conn: sqlite3.Connection, p: Period, account_ids: list[int] | None, today: date) -> dict:
    """Suivi de la période : une ligne par catégorie racine de dépense (plus « À classer »), totaux, rythme,
    et l'évolution objectif / dépensé des 12 mois finissant avec la période."""
    chart_months = [_shift(month_of(p.end), -i) for i in range(11, -1, -1)]
    spend, hist = _load(conn, min(p.start, _month_period(chart_months[0]).start), p.end, account_ids, today)
    habits, targets = Habits(spend, hist), Targets(conn)
    in_progress = p.start <= today <= p.end
    rows = []
    for cid, name in [*budget_categories(conn), (None, UNCLASSIFIED)]:
        target = expected = remaining = None
        spent = spend.between(cid, p)
        for mk in month_keys(p):
            mp = clip_month(p, mk)
            monthly = targets.get(cid, mk) if cid is not None else None
            if monthly is not None:
                target = (target or 0) + monthly * habits.share(cid, mk, mp.start.day, mp.end.day)
            if in_progress:
                if monthly is not None and mp.start <= today:
                    elapsed = habits.share(cid, mk, mp.start.day, min(mp.end, today).day)
                    expected = (expected or 0) + monthly * elapsed
                if mp.end > today:
                    left = habits.typical(cid, mk, max(mp.start, today + timedelta(days=1)).day, mp.end.day)
                    remaining = None if left is None else (remaining or 0) + left
        if in_progress and remaining is None and hist:
            remaining = 0.0
        projection = spent + remaining if in_progress and remaining is not None else None
        if cid is None and not spent and not projection:
            continue                                    # pas de ligne « À classer » vide
        target_c = _cents(target)
        rows.append({
            "id": cid, "name": name, "target": target_c, "spent": spent,
            "percent": None if not target_c else spent / target_c,
            "expected": _cents(expected), "projection": _cents(projection),
            "monthly_target": None if cid is None else targets.get(cid, month_of(min(max(today, p.start), p.end))),
            "status": _status(target_c, spent, expected, projection),
        })
    budgeted = [r for r in rows if r["target"] is not None]
    days_left = (p.end - today).days + 1 if in_progress else 0
    total_target = sum(r["target"] for r in budgeted)
    spent_budgeted = sum(r["spent"] for r in budgeted)
    proj = [r["projection"] for r in budgeted if r["projection"] is not None]
    monthly = []
    for mk in chart_months:
        cats = [(cid, targets.get(cid, mk)) for cid, _ in budget_categories(conn)]
        cats = [(cid, t) for cid, t in cats if t is not None]
        monthly.append({"month": mk, "target": sum(t for _, t in cats) if cats else None,
                        "spent": sum(spend.month(cid, mk) for cid, _ in cats),
                        "spent_all": sum(spend.month(cid, mk) for cid, _ in budget_categories(conn))
                        + spend.month(None, mk)})
    return {
        "today": today, "in_progress": in_progress, "days_left": days_left,
        "history_months": hist,
        "rows": rows,
        "totals": {
            "target": total_target if budgeted else None,
            "spent_budgeted": spent_budgeted,
            "spent_unbudgeted": sum(r["spent"] for r in rows if r["target"] is None),
            "expected": sum(r["expected"] or 0 for r in budgeted) if in_progress and budgeted else None,
            "projection": sum(proj) if in_progress and len(proj) == len(budgeted) and budgeted else None,
            "per_day": (total_target - spent_budgeted) / days_left if budgeted and days_left else None,
        },
        "monthly": monthly,
    }


# --- Assistant ----------------------------------------------------------------------------------

def _round_up(cents: float) -> int:
    return int(math.ceil(cents / ROUND_TO - 1e-9)) * ROUND_TO


def suggest(conn: sqlite3.Connection, account_ids: list[int] | None, today: date, *,
            income: int | None = None, savings: int = 0, effort: str = "medium") -> dict:
    """Objectifs proposés d'après les mois d'habitude ; rien n'est enregistré."""
    reduce = EFFORTS[effort]
    spend, hist = _load(conn, today, today, account_ids, today)
    n = len(hist)
    income_avg = (sum(totals(conn, _month_period(m), account_ids).income for m in hist) / n) if n else Decimal(0)
    income_avg_c = int(round(income_avg * 100))
    income_c = income_avg_c if income is None else income
    cats = []
    for cid, name in budget_categories(conn):
        amounts = [spend.month(cid, m) for m in hist]
        avg = sum(amounts) / n if n else 0
        present = sum(1 for a in amounts if a > 0)
        fixed = (n >= 3 and avg > 0 and present >= FIXED_PRESENCE * n
                 and statistics.pstdev(amounts) / avg <= FIXED_CV)
        base = avg if fixed else avg * (1 - reduce)
        cats.append({"id": cid, "name": name, "average": avg, "fixed": fixed, "base": max(base, 0)})
    available = income_c - savings
    total = sum(c["base"] for c in cats)
    variable = sum(c["base"] for c in cats if not c["fixed"])
    floor = sum(max(c["average"], 0) * VARIABLE_FLOOR for c in cats if not c["fixed"])
    feasible = True
    if total > available and variable > 0:
        need, room = total - available, max(variable - floor, 0)
        feasible = need <= room
        factor = (variable - min(need, room)) / variable
        for c in cats:
            if not c["fixed"]:
                c["base"] *= factor
    elif total > available:
        feasible = False
    for c in cats:
        c["suggested"] = _round_up(c["base"]) if c["average"] > 0 else None
    suggested_total = sum(c["suggested"] or 0 for c in cats)
    return {
        "history_months": hist,
        "income_average": income_avg_c, "income": income_c, "savings": savings, "effort": effort,
        "categories": [{"id": c["id"], "name": c["name"], "average": int(round(c["average"])), "fixed": c["fixed"],
                        "suggested": c["suggested"]} for c in cats],
        "total": suggested_total,
        "projected_savings": income_c - suggested_total,
        "unclassified_average": int(round(sum(spend.month(None, m) for m in hist) / n)) if n else 0,
        "feasible": feasible,
    }


# --- Ce qui coûte cher et conseils --------------------------------------------------------------

def merchant_name(label_norm: str) -> str:
    return _MERCHANT_PREFIX.sub("", label_norm) or label_norm


def _top_merchants(conn, p: Period, account_ids, limit: int = 10) -> list[dict]:
    where, params = scope_clause(p, account_ids)
    expense = (f"(c.kind = 'expense' OR (t.category_id IS NULL AND ({EFFECTIVE_KIND}) IS NULL "
               "AND t.amount_cents < 0))")
    rows = conn.execute(
        f"""SELECT t.label_norm, SUM(-t.amount_cents), SUM(t.amount_cents < 0) {ANALYSIS_FROM}
            WHERE {where} AND {expense} GROUP BY 1 HAVING SUM(-t.amount_cents) > 0
            ORDER BY 2 DESC, 1 LIMIT ?""", params + [limit]).fetchall()
    small = conn.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(-t.amount_cents), 0) {ANALYSIS_FROM}
            WHERE {where} AND {expense} AND t.amount_cents < 0 AND -t.amount_cents < ?""",
        params + [SMALL_PURCHASE_CENTS]).fetchone()
    return [{"label": label, "name": merchant_name(label), "total": total, "count": count}
            for label, total, count in rows], small


def insights(conn: sqlite3.Connection, p: Period, account_ids: list[int] | None, today: date) -> dict:
    empty = account_ids is not None and not account_ids
    spend, hist = _load(conn, p.start, p.end, account_ids, today)
    targets = Targets(conn)
    t = totals(conn, p, account_ids)
    expenses = int(t.expenses * 100)
    merchants, (small_count, small_total) = ([], (0, 0)) if empty else _top_merchants(conn, p, account_ids)
    months_in_period = len(month_keys(p))

    year = Period(_month_period(_shift(month_of(p.end), -11)).start, p.end)   # 12 mois finissant avec la période
    active_from = _shift(month_of(p.end), -1)
    subs = [r for r in recurring(conn, year, account_ids) if r["months"][-1] >= active_from]
    subscriptions = [{"label": r["label_norm"], "name": merchant_name(r["label_norm"]),
                      "monthly": int(r["avg_amount"] * 100), "yearly": int(r["avg_amount"] * 100) * 12,
                      "months": len(r["months"])} for r in subs]

    tips: list[dict] = []
    euros = lambda c: f"{c / 100:,.0f} €".replace(",", " ")       # noqa: E731
    cats = budget_categories(conn)
    last3 = [m for m in hist[-3:]] if len(hist) >= 3 else []
    for cid, name in cats:
        if not last3:
            break
        pairs = [(spend.month(cid, m), targets.get(cid, m)) for m in last3]
        if any(target is None for _, target in pairs):
            continue
        spent_avg = sum(s for s, _ in pairs) / 3
        target_avg = sum(tg for _, tg in pairs) / 3
        if all(s > tg for s, tg in pairs):
            tips.append({"level": "warn", "text": f"« {name} » a dépassé son objectif ces 3 derniers mois "
                         f"({euros(spent_avg)} en moyenne pour {euros(target_avg)}) : l'objectif est peut-être "
                         "trop serré, ou c'est le poste à surveiller en priorité."})
        elif target_avg > 0 and all(s < 0.7 * tg for s, tg in pairs):
            lower = _round_up(spent_avg * 1.1)
            tips.append({"level": "good", "text": f"« {name} » reste bien sous son objectif depuis 3 mois "
                         f"({euros(spent_avg)} pour {euros(target_avg)}) : le ramener à {euros(lower)} libérerait "
                         f"{euros(target_avg - lower)} par mois pour l'épargne."})

    by_cat = {cid: spend.between(cid, p) for cid, _ in cats}
    for cid, name in cats:
        if "FRAIS BANCAIRES" in fold(name) and by_cat[cid] > 0:
            tips.append({"level": "warn", "text": f"{euros(by_cat[cid])} de frais bancaires sur la période : une "
                         "banque en ligne ou une formule renégociée peut les supprimer presque entièrement."})
    if hist:
        for cid, name in cats:
            usual = sum(spend.month(cid, m) for m in hist) / len(hist)
            now = by_cat[cid] / months_in_period
            if usual > 0 and now > usual * 1.2 and now - usual >= 3000 and p.end < today:   # période terminée
                tips.append({"level": "warn", "text": f"« {name} » : {euros(now)} par mois sur la période, "
                             f"+{(now / usual - 1) * 100:.0f} % par rapport à ton habitude ({euros(usual)})."})
    if subscriptions:
        yearly = sum(s["yearly"] for s in subscriptions)
        tips.append({"level": "info", "text": f"Tes dépenses récurrentes représentent {euros(yearly)} par an. "
                     "Passe-les en revue : chaque abonnement de 10 € par mois résilié, c'est 120 € par an."})
    if expenses > 0 and small_total > 0.05 * expenses:
        tips.append({"level": "info", "text": f"{small_count} petits achats de moins de 15 € totalisent "
                     f"{euros(small_total)} ({small_total / expenses * 100:.0f} % des dépenses) : c'est souvent là "
                     "que le budget fuit sans qu'on le voie."})
    income = int(t.income * 100)
    if income > 0:
        rate = int(t.savings_net * 100) / income
        if rate < 0.10:
            tips.append({"level": "warn", "text": f"Tu as épargné {rate * 100:.0f} % de tes revenus sur la période ; "
                         "le repère courant (règle 50/30/20) est de 20 %."})
        elif rate >= 0.20:
            tips.append({"level": "good", "text": f"Tu as épargné {rate * 100:.0f} % de tes revenus sur la "
                         "période : au-dessus du repère de 20 %, bravo."})
    unclassified = int(t.unclassified_debits * 100)
    if expenses > 0 and unclassified > 0.05 * expenses:
        tips.append({"level": "info", "text": f"{euros(unclassified)} de dépenses sont encore à classer : classe-les "
                     "pour un suivi de budget fiable."})
    heavy = sorted(((amt, name) for (cid, name) in cats for amt in [by_cat[cid]] if amt > 0), reverse=True)[:3]
    for amount, name in heavy:
        for key, tip in CATEGORY_TIPS.items():
            if fold(name).startswith(key):
                tips.append({"level": "tip", "text": f"{name} ({euros(amount)}) : {tip}"})
    return {
        "merchants": merchants,
        "small_purchases": {"count": small_count, "total": small_total,
                            "per_month": small_total / months_in_period if months_in_period else 0},
        "subscriptions": subscriptions,
        "subscriptions_yearly": sum(s["yearly"] for s in subscriptions),
        "expenses": expenses,
        "tips": tips,
    }


# --- Objectifs : lecture et enregistrement ------------------------------------------------------

def targets_for(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Objectif de chaque catégorie budgétable pour `month`, et le mois depuis lequel il s'applique."""
    targets = Targets(conn)
    return [{"id": cid, "name": name, "amount": targets.get(cid, month), "since": targets.since(cid, month)}
            for cid, name in budget_categories(conn)]


def changes(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Historique des changements d'objectif, du plus récent au plus ancien (avec la valeur précédente)."""
    rows = conn.execute(
        """SELECT b.category_id, c.name, b.effective_from, b.monthly_cents, b.created_at,
                  (SELECT p.monthly_cents FROM budget_targets p WHERE p.category_id = b.category_id
                     AND p.effective_from < b.effective_from ORDER BY p.effective_from DESC LIMIT 1)
           FROM budget_targets b JOIN categories c ON c.id = b.category_id
           ORDER BY b.effective_from DESC, b.created_at DESC, c.name LIMIT ?""", (limit,)).fetchall()
    return [{"category_id": cid, "name": name, "month": month, "amount": cents, "previous": previous,
             "changed_at": created} for cid, name, month, cents, created, previous in rows]


def save_targets(conn: sqlite3.Connection, from_month: str, values: dict[int, int | None], now: str) -> int:
    """Applique `values` ({catégorie: centimes ou None}) à partir de `from_month` ; ne valide pas.

    Pour chaque catégorie dont l'objectif change à ce mois, les changements datés de ce mois ou après sont
    remplacés (le nouveau montant vaut « à partir de ce mois ») ; aucune ligne n'est écrite si le montant égale
    celui qui s'appliquait déjà avant. Renvoie le nombre de catégories modifiées. ValueError si une catégorie
    n'est pas une racine de dépense."""
    allowed = {cid for cid, _ in budget_categories(conn)}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"catégorie(s) non budgétable(s) : {sorted(unknown)}")
    targets = Targets(conn)
    changed = 0
    for cid, cents in values.items():
        if targets.get(cid, from_month) == cents:
            continue
        conn.execute("DELETE FROM budget_targets WHERE category_id = ? AND effective_from >= ?", (cid, from_month))
        if targets.get(cid, _shift(from_month, -1)) != cents:
            conn.execute("INSERT INTO budget_targets(category_id, effective_from, monthly_cents, created_at) "
                         "VALUES (?, ?, ?, ?)", (cid, from_month, cents, now))
        changed += 1
    return changed
