from datetime import date
from decimal import Decimal

from app.importer.validate import reconciliation_gap
from app.model import ParsedSection, ParsedTransaction


def _tx(amount: str) -> ParsedTransaction:
    return ParsedTransaction(date(2025, 1, 5), date(2025, 1, 5), "OP", Decimal(amount))


def _section(opening: str, closing: str, amounts: list[str]) -> ParsedSection:
    return ParsedSection(
        account_label="COMPTE TEST",
        account_number="00000000001",
        statement_date=date(2025, 1, 31),
        opening=Decimal(opening),
        closing=Decimal(closing),
        transactions=[_tx(a) for a in amounts],
    )


def test_coherent_section_gap_is_zero():
    s = _section("100.00", "89.50", ["-20.00", "9.50"])
    assert reconciliation_gap(s) == Decimal("0.00")


def test_missing_amount_gives_negative_gap():
    s = _section("100.00", "87.00", ["-10.50"])
    # attendu: 100 - 10.50 = 89.50 ; clôture 87.00 -> écart -2.50
    assert reconciliation_gap(s) == Decimal("-2.50")


def test_empty_section_with_equal_balances():
    s = _section("42.10", "42.10", [])
    assert reconciliation_gap(s) == Decimal("0.00")
