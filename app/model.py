from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class ParsedTransaction:
    op_date: date
    value_date: date
    label: str
    amount: Decimal  # < 0 débit, > 0 crédit


@dataclass
class ParsedSection:
    account_label: str
    account_number: str
    statement_date: date
    opening: Decimal  # signé (DEBITEUR -> négatif)
    closing: Decimal  # signé (DEBITEUR -> négatif)
    transactions: list[ParsedTransaction]
    info_lines: int = 0  # entrées datées sans montant (avis d'information), ignorées


@dataclass
class ParsedDocument:
    statement_date: date
    sections: list[ParsedSection]


class ParseError(Exception):
    """Le document ne peut pas être interprété."""
