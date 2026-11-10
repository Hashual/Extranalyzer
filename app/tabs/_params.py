"""Paramètres communs aux onglets : période, période de comparaison et comptes.

`scope` est une dépendance FastAPI : `start`/`end` (dates ISO, obligatoires, start <= end),
`accounts` (ids séparés par des virgules ; absent ou vide -> None = vue par défaut des comptes
courants), `cstart`/`cend` (comparaison, les deux ou aucun). Toute incohérence -> 422.
"""
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, Query

from app.analytics.periods import Period


@dataclass(frozen=True)
class Scope:
    period: Period
    compare: Period | None
    account_ids: list[int] | None


def _period(start: date, end: date, what: str) -> Period:
    if start > end:
        raise HTTPException(422, f"{what} : la date de début est postérieure à la date de fin")
    return Period(start, end)


def _accounts(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None
    try:
        return [int(part) for part in raw.split(",")]
    except ValueError:
        raise HTTPException(422, "accounts : liste d'identifiants séparés par des virgules attendue") from None


def scope(start: date = Query(), end: date = Query(), accounts: str | None = Query(None),
          cstart: date | None = Query(None), cend: date | None = Query(None)) -> Scope:
    if (cstart is None) != (cend is None):
        raise HTTPException(422, "cstart et cend vont ensemble")
    compare = None if cstart is None else _period(cstart, cend, "période de comparaison")
    return Scope(_period(start, end, "période"), compare, _accounts(accounts))

