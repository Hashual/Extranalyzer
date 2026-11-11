import re
from decimal import Decimal

_AMOUNT_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")


def is_amount(text: str) -> bool:
    return _AMOUNT_RE.fullmatch(text) is not None


def parse_amount(text: str) -> Decimal:
    if not is_amount(text):
        raise ValueError(f"montant invalide : {text!r}")
    return Decimal(text.replace(".", "").replace(",", "."))


def to_cents(d: Decimal) -> int:
    return int(d.scaleb(2).to_integral_value())


def from_cents(c: int) -> Decimal:
    return Decimal(c).scaleb(-2)
