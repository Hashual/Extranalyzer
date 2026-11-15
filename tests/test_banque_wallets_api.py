"""Routes des portefeuilles Solana de l'onglet Banque (faux Helius, données fictives)."""
import threading

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import Settings, SolanaSettings
from app.main import create_app
from app.sources.solana.holdings import EURC_MINT
from app.sources.solana.runtime import RefreshBusy
from tests.fakehelius import FakeHelius
from tests.test_solana_holdings import fungible

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
KEY = "cle-secrete-fictive"
ADDR_A = "11111111111111111111111111111112"
ADDR_B = "11111111111111111111111111111113"
MINT = "11111111111111111111111111111114"
VOTE = "11111111111111111111111111111115"
URL = "/api/tabs/banque/wallets"


def _fake():
    fake = FakeHelius()
    fake.on("getAssetsByOwner", {"items": [fungible(EURC_MINT, 0, 6, 1.08, "EURC"),
                                           fungible(MINT, 5_000_000, 6, 2.0, "JET")],
                                 "nativeBalance": {"lamports": 2_000_000_000, "price_per_sol": 150.0}})
    fake.on("getProgramAccounts", [])
    return fake


def _client(tmp_path, solana=True):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1,
                        solana=SolanaSettings(KEY) if solana else None)
    client = TestClient(create_app(settings, solana_transport=_fake()))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return client


@pytest.fixture
def client(tmp_path):
    return _client(tmp_path)


def _add(client, label="Principal", address=ADDR_A):
    return client.post(URL, json={"label": label, "address": address}, headers=H)


def _no_key(response):
    assert KEY not in response.text and "api-key" not in response.text


def test_get_not_configured(tmp_path):
    client = _client(tmp_path, solana=False)
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json()["configured"] is False and r.json()["wallets"] == []
    assert client.get("/api/tabs/banque/status").status_code == 200
    r = _add(client)
    assert r.status_code == 200 and r.json()["refresh"] is None
    assert client.post(f"{URL}/refresh", json={}, headers=H).status_code == 503


def test_add_refreshes_and_lists(client):
    r = _add(client)
    assert r.status_code == 200
    assert r.json()["refresh"] == {"refreshed": 1, "errors": 0}
    data = client.get(URL).json()
    assert data["configured"] is True
    (wallet,) = data["wallets"]
    assert wallet["id"] == r.json()["id"] and wallet["category_name"] == "Principal"
    assert wallet["positions"] and wallet["last_error"] is None
    _no_key(client.get(URL))


def test_add_invalid_address_422(client):
    r = _add(client, address="0x" + "ab" * 20)
    assert r.status_code == 422
    _no_key(r)


def test_add_strips_spaces(client):
    r = _add(client, address=f"  {ADDR_A} ")
    assert r.status_code == 200
    assert client.get(URL).json()["wallets"][0]["address"] == ADDR_A


def test_add_duplicate_409(client):
    assert _add(client).status_code == 200
    r = _add(client, label="Autre")
    assert r.status_code == 409
    _no_key(r)


def test_rename_and_delete(client):
    wid = _add(client).json()["id"]
    assert client.patch(f"{URL}/{wid}", json={"label": "Nouveau"}, headers=H).json() == {"ok": True}
    assert client.get(URL).json()["wallets"][0]["label"] == "Nouveau"
    assert client.patch(f"{URL}/{wid}", json={"label": " "}, headers=H).status_code == 422
    assert client.patch(f"{URL}/999", json={"label": "X"}, headers=H).status_code == 404
    assert client.delete(f"{URL}/{wid}", headers=H).json() == {"ok": True}
    assert client.get(URL).json()["wallets"] == []


def test_delete_unknown_404(client):
    r = client.delete(f"{URL}/999", headers=H)
    assert r.status_code == 404
    _no_key(r)


def test_refresh_one_and_all(client):
    wid = _add(client).json()["id"]
    assert client.post(f"{URL}/refresh", json={}, headers=H).json() == {"refreshed": 1, "errors": 0}
    assert client.post(f"{URL}/refresh", json={"wallet_id": wid}, headers=H).json() == {"refreshed": 1, "errors": 0}


def test_refresh_busy_409(client, monkeypatch):
    def busy(*args, **kwargs):
        raise RefreshBusy("une actualisation est déjà en cours")
    monkeypatch.setattr(client.app.state.solana, "refresh", busy)
    r = client.post(f"{URL}/refresh", json={}, headers=H)
    assert r.status_code == 409 and "déjà en cours" in r.json()["detail"]
    _no_key(r)


def test_refresh_not_configured_503(tmp_path):
    client = _client(tmp_path, solana=False)
    r = client.post(f"{URL}/refresh", json={}, headers=H)
    assert r.status_code == 503 and "non configurés" in r.json()["detail"]
    _no_key(r)


def test_validator_label_and_hidden_mint(client):
    _add(client)
    before = client.get(URL).json()["total_eur"]
    assert client.put(f"{URL}/validators/{VOTE}", json={"label": "Mon validateur"}, headers=H).json() == {"ok": True}
    assert client.get(URL).json()["validators"] == {VOTE: "Mon validateur"}
    assert client.put(f"{URL}/hidden-mints/{MINT}", json={"hidden": True}, headers=H).json() == {"ok": True}
    data = client.get(URL).json()
    assert data["hidden_mints"] == [MINT] and float(data["total_eur"]) < float(before)
    assert client.put(f"{URL}/hidden-mints/{MINT}", json={"hidden": False}, headers=H).status_code == 200
    assert client.get(URL).json()["hidden_mints"] == []
    assert client.put(f"{URL}/validators/pas-une-adresse", json={"label": "x"}, headers=H).status_code == 422
    assert client.put(f"{URL}/hidden-mints/0xabc", json={"hidden": True}, headers=H).status_code == 422


def test_key_never_in_responses(client):
    responses = [_add(client), _add(client), _add(client, address="nope"), client.get(URL),
                 client.patch(f"{URL}/999", json={"label": "X"}, headers=H), client.delete(f"{URL}/999", headers=H),
                 client.post(f"{URL}/refresh", json={}, headers=H),
                 client.put(f"{URL}/validators/{VOTE}", json={"label": "v"}, headers=H),
                 client.put(f"{URL}/hidden-mints/bad", json={"hidden": True}, headers=H)]
    for r in responses:
        _no_key(r)


def test_csrf_required(client):
    assert client.post(URL, json={"label": "A", "address": ADDR_A}).status_code == 403
    assert client.get(URL).status_code == 200


def test_quantities_and_prices_are_not_rounded(tmp_path):
    fake = FakeHelius()
    fake.on("getAssetsByOwner", {"items": [fungible(EURC_MINT, 0, 6, 1.0, "EURC"),
                                           fungible(MINT, 22_105_000, 6, 0.00012, "TINY")],
                                 "nativeBalance": {"lamports": 0}})
    fake.on("getProgramAccounts", [])
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1,
                        solana=SolanaSettings(KEY))
    client = TestClient(create_app(settings, solana_transport=fake))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    _add(client)
    positions = client.get(URL).json()["wallets"][0]["positions"]
    tiny = next(p for p in positions if p["mint"] == MINT)
    assert tiny["amount"] == "22.105"
    assert tiny["price_eur"] == "0.00012"
