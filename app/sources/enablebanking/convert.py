"""Traduction des réponses Enable Banking en objets communs des sources (réponses traitées comme non fiables)."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.sources.base import FetchedBalance, FetchedTransaction

CURRENCY = "EUR"
BOOKED_BALANCES = ("CLBD", "ITBD")    # soldes comptables : les soldes « disponibles » incluent les opérations en attente


def _date(value) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _amount(value) -> tuple[Decimal, object] | None:
    if not isinstance(value, dict) or not isinstance(value.get("amount"), (str, int, float)):
        return None
    try:
        amount = Decimal(str(value["amount"]))
    except InvalidOperation:
        return None
    return (amount, value.get("currency")) if amount.is_finite() else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_booked(tx: dict) -> bool:
    return tx.get("status", "BOOK") == "BOOK"


def label_of(tx: dict) -> str:
    """Libellé : informations de remise, sinon nom de la contrepartie, sinon description du code d'opération."""
    remittance = tx.get("remittance_information")
    if isinstance(remittance, str):
        remittance = [remittance]
    if isinstance(remittance, list):
        text = _clean(" ".join(r for r in remittance if isinstance(r, str)))
        if text:
            return text
    party = tx.get("creditor") if tx.get("credit_debit_indicator") == "DBIT" else tx.get("debtor")
    if isinstance(party, dict) and isinstance(party.get("name"), str) and _clean(party["name"]):
        return _clean(party["name"])
    code = tx.get("bank_transaction_code")
    if isinstance(code, dict) and isinstance(code.get("description"), str) and _clean(code["description"]):
        return _clean(code["description"])
    return "OPERATION SANS LIBELLE"


def convert_transaction(tx: dict) -> FetchedTransaction | None:
    """Opération en euros, ou None si elle est inutilisable (devise, montant ou date absents)."""
    parsed = _amount(tx.get("transaction_amount"))
    if parsed is None or parsed[1] != CURRENCY:
        return None
    amount = abs(parsed[0])
    indicator = tx.get("credit_debit_indicator")
    if indicator == "DBIT" or (indicator != "CRDT" and parsed[0] < 0):
        amount = -amount
    op_date = _date(tx.get("booking_date")) or _date(tx.get("transaction_date")) or _date(tx.get("value_date"))
    if op_date is None:
        return None
    return FetchedTransaction(op_date, _date(tx.get("value_date")), label_of(tx), amount, tx)


def pick_balance(balances: list[dict]) -> FetchedBalance | None:
    """Solde comptable en euros (CLBD, sinon ITBD) avec sa date ; None si aucun."""
    for kind in BOOKED_BALANCES:
        for b in balances:
            if not isinstance(b, dict) or b.get("balance_type") != kind:
                continue
            parsed = _amount(b.get("balance_amount"))
            day = _date(b.get("reference_date")) or _date(b.get("last_change_date_time"))
            if parsed is not None and parsed[1] == CURRENCY and day is not None:
                return FetchedBalance(parsed[0], day)
    return None
