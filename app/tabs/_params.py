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


def scope_where(s: Scope, *, include_mirrors: bool = False) -> tuple[str, list]:
    """Clause WHERE sur `transactions t JOIN accounts a`, avec les mêmes règles que les calculs
    d'analyse (account_ids None -> comptes courants). Jambes miroir exclues sauf `include_mirrors`."""
    clause = "t.op_date BETWEEN ? AND ?"
    params: list = [s.period.start.isoformat(), s.period.end.isoformat()]
    if not include_mirrors:
        clause += " AND t.mirror_of IS NULL"
    if s.account_ids is None:
        clause += " AND a.kind = 'checking'"
    else:
        clause += f" AND t.account_id IN ({','.join('?' * len(s.account_ids))})"
        params += s.account_ids
    return clause, params
