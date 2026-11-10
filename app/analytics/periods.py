from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # bornes incluses


def previous_period(p: Period) -> Period:
    """Période de même durée (en jours) située juste avant `p`."""
    days = (p.end - p.start).days + 1
    prev_end = p.start - timedelta(days=1)
    return Period(prev_end - timedelta(days=days - 1), prev_end)


def month_keys(p: Period) -> list[str]:
    """Tous les mois 'YYYY-MM' qui intersectent la période."""
    keys = []
    y, m = p.start.year, p.start.month
    while (y, m) <= (p.end.year, p.end.month):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


def clip_month(p: Period, key: str) -> Period:
    """Intersection du mois `key` avec la période."""
    y, m = int(key[:4]), int(key[5:])
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return Period(max(first, p.start), min(nxt - timedelta(days=1), p.end))
