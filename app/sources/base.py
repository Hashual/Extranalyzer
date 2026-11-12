"""Objets communs à toutes les sources externes : ce qu'une source a récupéré pour un compte Extranalyzer.

Une source (Enable Banking aujourd'hui, Kraken plus tard) traduit son API en ces objets ; `app.sources.service`
les écrit en base sans rien savoir de la source.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class FetchedTransaction:
    op_date: date
    value_date: date | None
    label: str
    amount: Decimal          # < 0 débit, > 0 crédit
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class FetchedBalance:
    amount: Decimal          # solde comptable signé
    reference_date: date


@dataclass
class AccountFetch:
    """Résultat de la récupération d'un compte. `error` non nul : rien n'est écrit pour ce compte."""
    account_id: int
    transactions: list[FetchedTransaction]
    balance: FetchedBalance | None = None
    rejected: int = 0        # opérations reçues mais inutilisables (devise, date absente…)
    error: str | None = None
    external_id: int | None = None   # ligne `external_accounts` d'origine, le cas échéant
