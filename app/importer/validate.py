from decimal import Decimal

from app.model import ParsedSection


def reconciliation_gap(section: ParsedSection) -> Decimal:
    """closing - (opening + somme des montants) ; Decimal('0.00') signifie OK."""
    total = sum((t.amount for t in section.transactions), Decimal("0.00"))
    return section.closing - (section.opening + total)
