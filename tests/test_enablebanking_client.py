"""Jeton JWT, client HTTP (transport simulé) et conversion des réponses Enable Banking (données fictives)."""
import base64
import json
from datetime import date
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.sources.enablebanking.client import Client, EnableBankingError, RateLimited, SessionExpired
from app.sources.enablebanking.convert import convert_transaction, is_booked, label_of, pick_balance
from app.sources.enablebanking.jwt import make_jwt
from fakebank import FakeBank, booked, rsa_key, rsa_key_pem

NOW = 1_800_000_000


def _part(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def test_jwt_header_claims_and_signature():
    token = make_jwt("app-fictive", rsa_key(), NOW)
    header, payload, signature = token.split(".")
    assert _part(header) == {"typ": "JWT", "alg": "RS256", "kid": "app-fictive"}
    assert _part(payload) == {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": NOW,
                              "exp": NOW + 3600}
    rsa_key().public_key().verify(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
                                   f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_jwt_lifetime_capped_at_24h():
    with pytest.raises(ValueError):
        make_jwt("app", rsa_key(), NOW, lifetime=86401)


def _client(bank: FakeBank) -> Client:
    return Client("app-fictive", rsa_key_pem(), transport=bank, clock=lambda: NOW)


def test_requests_carry_bearer_token():
    bank = FakeBank().on("GET", "/aspsps", {"aspsps": [{"name": "Banque fictive", "country": "FR"}]})
    assert _client(bank).aspsps("FR") == [{"name": "Banque fictive", "country": "FR"}]
    request = bank.requests[0]
    assert request["query"] == {"country": "FR", "psu_type": "personal"}
    assert request["headers"]["Authorization"].startswith("Bearer ey")


def test_start_auth_body():
    bank = FakeBank().on("POST", "/auth", {"url": "https://banque.example/auth", "authorization_id": "a"})
    _client(bank).start_auth("Banque fictive", "FR", "2027-05-01T00:00:00+00:00", "etat", "http://10.0.0.1/cb")
    assert bank.requests[0]["body"] == {
        "access": {"valid_until": "2027-05-01T00:00:00+00:00"}, "aspsp": {"name": "Banque fictive", "country": "FR"},
        "state": "etat", "redirect_url": "http://10.0.0.1/cb", "psu_type": "personal"}


def test_transactions_follow_continuation_key():
    bank = FakeBank().on("GET", "/accounts/u1/transactions",
                         {"transactions": [booked("2025-03-01", "1.00", "A")], "continuation_key": "p2"},
                         {"transactions": [booked("2025-03-02", "2.00", "B")], "continuation_key": None})
    found = _client(bank).transactions("u1", date(2025, 3, 1), "longest")
    assert [t["remittance_information"] for t in found] == [["A"], ["B"]]
    assert [r["query"] for r in bank.requests] == [
        {"date_from": "2025-03-01", "strategy": "longest"},
        {"date_from": "2025-03-01", "strategy": "longest", "continuation_key": "p2"}]


@pytest.mark.parametrize("status, body, kind", [
    (401, {"error": "EXPIRED_SESSION", "message": "Session expired"}, SessionExpired),
    (429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "Too many"}, RateLimited),
    (500, {"message": "boom"}, EnableBankingError),
    (502, "pas du json", EnableBankingError),
])
def test_errors_are_typed(status, body, kind):
    bank = FakeBank().on("GET", "/accounts/u1/balances", (status, body))
    with pytest.raises(kind) as info:
        _client(bank).balances("u1")
    assert info.value.status == status


def test_unauthorized_without_session_code_is_not_expiry():
    bank = FakeBank().on("GET", "/aspsps", (401, {"error": "INVALID_JWT", "message": "bad"}))
    with pytest.raises(EnableBankingError) as info:
        _client(bank).aspsps("FR")
    assert not isinstance(info.value, SessionExpired)


def test_convert_debit_and_credit():
    debit = convert_transaction(booked("2025-03-01", "12.34", "CB MAGASIN FICTIF"))
    assert (debit.op_date, debit.amount, debit.label) == (date(2025, 3, 1), Decimal("-12.34"), "CB MAGASIN FICTIF")
    credit = convert_transaction(booked("2025-03-02", "50", "VIR FICTIF", indicator="CRDT"))
    assert credit.amount == Decimal("50")


def test_convert_rejects_foreign_currency_and_missing_date():
    tx = booked("2025-03-01", "1.00", "X")
    assert convert_transaction({**tx, "transaction_amount": {"amount": "1.00", "currency": "USD"}}) is None
    assert convert_transaction({**tx, "booking_date": None, "value_date": None}) is None
    assert convert_transaction({**tx, "transaction_amount": {"amount": "abc", "currency": "EUR"}}) is None


def test_convert_date_fallbacks():
    tx = {**booked("2025-03-01", "1.00", "X"), "booking_date": None, "transaction_date": "2025-02-28"}
    assert convert_transaction(tx).op_date == date(2025, 2, 28)


def test_label_fallbacks():
    tx = booked("2025-03-01", "1.00", "  CB   MAGASIN \n FICTIF ")
    assert label_of(tx) == "CB MAGASIN FICTIF"
    no_text = {**tx, "remittance_information": []}
    assert label_of({**no_text, "creditor": {"name": "Créancier fictif"}}) == "Créancier fictif"
    assert label_of({**no_text, "bank_transaction_code": {"description": "Paiement"}}) == "Paiement"
    assert label_of(no_text) == "OPERATION SANS LIBELLE"


def test_only_booked():
    assert is_booked(booked("2025-03-01", "1", "X"))
    assert not is_booked({**booked("2025-03-01", "1", "X"), "status": "PDNG"})


def test_pick_balance_prefers_booked():
    balances = [
        {"balance_type": "ITAV", "balance_amount": {"amount": "999", "currency": "EUR"}, "reference_date": "2025-03-05"},
        {"balance_type": "ITBD", "balance_amount": {"amount": "-10.5", "currency": "EUR"}, "reference_date": "2025-03-04"},
        {"balance_type": "CLBD", "balance_amount": {"amount": "120.00", "currency": "EUR"}, "reference_date": "2025-03-03"},
    ]
    assert pick_balance(balances).amount == Decimal("120.00")
    assert pick_balance(balances[:2]).amount == Decimal("-10.5")
    assert pick_balance(balances[:1]) is None
