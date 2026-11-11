from decimal import Decimal

import pytest

from app.importer.amounts import from_cents, is_amount, parse_amount, to_cents


@pytest.mark.parametrize(
    "s,v",
    [("2,50", "2.50"), ("5.686,82", "5686.82"), ("1.234.567,00", "1234567.00")],
)
def test_parse_amount(s, v):
    assert parse_amount(s) == Decimal(v)


def test_is_amount_accepts():
    assert is_amount("2,50") and is_amount("5.686,82")


def test_is_amount_rejects():
    assert not is_amount("12/03/2024")
    assert not is_amount("0794")
    assert not is_amount("2,5")


def test_cents_roundtrip():
    assert from_cents(to_cents(Decimal("-1234.56"))) == Decimal("-1234.56")


def test_to_cents_is_int():
    assert to_cents(Decimal("2.50")) == 250


def test_parse_amount_invalid():
    with pytest.raises(ValueError):
        parse_amount("abc")
