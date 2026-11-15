"""Résultat de la lecture d'un ticket de caisse (indépendant de l'enseigne)."""
from dataclasses import dataclass
from datetime import datetime


class ReceiptParseError(ValueError):
    """Ticket illisible ou incohérent ; le message est affichable à l'utilisateur (sans donnée de ticket)."""


@dataclass
class ReceiptLine:
    aisle: str                  # rayon lu sur le ticket
    label: str
    quantity: float
    unit_cents: int | None
    amount_cents: int           # négatif pour une remise
    kind: str                   # 'item' | 'discount'


@dataclass
class Payment:
    method: str                 # card | meal_voucher | voucher | cash | other
    amount_cents: int


@dataclass
class ParsedReceipt:
    shop: str
    purchased_at: datetime
    total_cents: int
    lines: list[ReceiptLine]
    payments: list[Payment]
