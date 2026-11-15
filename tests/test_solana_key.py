"""Clé Helius saisie dans l'interface : vérification, enregistrement, priorité de la clé du serveur (faux Helius)."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import Settings, SolanaSettings, _solana_settings
from app.main import create_app
from app.sources.solana.runtime import KeyInvalid, KeyManagedByServer, SolanaRuntime
from tests.fakehelius import FakeHelius
from tests.test_banque_wallets_api import ADDR_A, H, PASSWORD, URL, _fake

NEW_KEY = "nouvelle-cle-fictive-1234"
SERVER_KEY = "cle-serveur-fictive"


def _noop(fn):
    raise AssertionError("pas d'accès base attendu")


def _helius(epoch_response=None):
    fake = _fake()
    fake.on("getEpochInfo", epoch_response if epoch_response is not None else {"epoch": 700})
    return fake


def _client(tmp_path, transport, server_key=False):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1,
                        solana=SolanaSettings(SERVER_KEY) if server_key else None)
    client = TestClient(create_app(settings, solana_transport=transport))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return client, settings.data_dir / "helius.key"


def _absent(response, *secrets):
    for secret in secrets:
        assert secret not in response.text
    assert "api-key" not in response.text


# --- runtime ---------------------------------------------------------------------------------------------------

def test_set_key_checks_then_stores(tmp_path):
    fake = _helius()
    key_file = tmp_path / "helius.key"
    runtime = SolanaRuntime(None, _noop, _noop, transport=fake, key_file=key_file)
    assert not runtime.configured and runtime.can_set_key
    runtime.set_key(f"  {NEW_KEY}\n")
    assert runtime.configured and runtime.key_source == "app"
    assert key_file.read_text(encoding="utf-8") == NEW_KEY
    assert fake.calls("getEpochInfo")[0]["query"]["api-key"] == NEW_KEY
    assert not (tmp_path / "helius.key.tmp").exists()
    # relu au démarrage suivant
    again = SolanaRuntime(None, _noop, _noop, transport=fake, key_file=key_file)
    assert again.configured and again.key_source == "app"


@pytest.mark.parametrize("bad", ["", "   ", "court", "https://mainnet.helius-rpc.com/?api-key=abcdefgh1234",
                                 "cle avec espaces dedans"])
def test_set_key_rejects_malformed(tmp_path, bad):
    fake = _helius()
    runtime = SolanaRuntime(None, _noop, _noop, transport=fake, key_file=tmp_path / "helius.key")
    with pytest.raises(KeyInvalid):
        runtime.set_key(bad)
    assert not fake.calls("getEpochInfo") and not runtime.configured
    assert not (tmp_path / "helius.key").exists()


@pytest.mark.parametrize("status", [401, 403])
def test_set_key_refused_by_helius_not_stored(tmp_path, status):
    fake = _helius((status, {"error": {"code": -32401, "message": f"invalid api key {NEW_KEY}"}}))
    runtime = SolanaRuntime(None, _noop, _noop, transport=fake, key_file=tmp_path / "helius.key")
    with pytest.raises(KeyInvalid) as info:
        runtime.set_key(NEW_KEY)
    assert NEW_KEY not in str(info.value)
    assert not runtime.configured and not (tmp_path / "helius.key").exists()


def test_server_key_wins_and_is_not_replaceable(tmp_path):
    key_file = tmp_path / "helius.key"
    key_file.write_text(NEW_KEY, encoding="utf-8")
    runtime = SolanaRuntime(SolanaSettings(SERVER_KEY), _noop, _noop, transport=_helius(), key_file=key_file)
    assert runtime.key_source == "server" and not runtime.can_set_key
    with pytest.raises(KeyManagedByServer):
        runtime.set_key(NEW_KEY)
    with pytest.raises(KeyManagedByServer):
        runtime.clear_key()


def test_clear_key_forgets_it(tmp_path):
    key_file = tmp_path / "helius.key"
    runtime = SolanaRuntime(None, _noop, _noop, transport=_helius(), key_file=key_file)
    runtime.set_key(NEW_KEY)
    runtime.clear_key()
    assert not runtime.configured and runtime.key_source is None and not key_file.exists()


def test_invalid_url_disables_key_entry(tmp_path):
    runtime = SolanaRuntime(None, _noop, _noop, transport=_helius(), key_file=tmp_path / "helius.key", base_url=None)
    assert not runtime.can_set_key


def test_server_settings_absent_by_default_is_silent(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("EXTRANALYZER_HELIUS_KEY_FILE", raising=False)
    monkeypatch.delenv("EXTRANALYZER_HELIUS_URL", raising=False)
    monkeypatch.setattr("app.config.DEFAULT_HELIUS_KEY_FILE", str(tmp_path / "absent"))
    with caplog.at_level(logging.WARNING):
        assert _solana_settings() is None
    assert not caplog.records


# --- routes ----------------------------------------------------------------------------------------------------

def test_key_entered_in_ui_enables_wallets(tmp_path):
    client, key_file = _client(tmp_path, _helius())
    data = client.get(URL).json()
    assert data["configured"] is False and data["can_set_key"] is True and data["key_source"] is None
    assert client.post(URL, json={"label": "Principal", "address": ADDR_A}, headers=H).status_code == 200
    r = client.put(f"{URL}/key", json={"api_key": NEW_KEY}, headers=H)
    assert r.status_code == 200 and r.json()["refresh"] == {"refreshed": 1, "errors": 0}
    _absent(r, NEW_KEY)
    data = client.get(URL)
    assert data.json()["configured"] is True and data.json()["key_source"] == "app"
    assert data.json()["wallets"][0]["positions"]
    _absent(data, NEW_KEY)
    assert key_file.exists()


def test_key_refused_422_and_not_stored(tmp_path):
    client, key_file = _client(tmp_path, _helius((401, {"error": {"code": -32401, "message": "bad key"}})))
    r = client.put(f"{URL}/key", json={"api_key": NEW_KEY}, headers=H)
    assert r.status_code == 422 and "refusée" in r.json()["detail"]
    _absent(r, NEW_KEY)
    assert not key_file.exists() and client.get(URL).json()["configured"] is False


def test_key_helius_unreachable_502(tmp_path):
    client, key_file = _client(tmp_path, _helius((500, {"error": {"code": -1, "message": f"panne {NEW_KEY}"}})))
    r = client.put(f"{URL}/key", json={"api_key": NEW_KEY}, headers=H)
    assert r.status_code == 502
    _absent(r, NEW_KEY)
    assert not key_file.exists()


def test_delete_key(tmp_path):
    client, key_file = _client(tmp_path, _helius())
    assert client.put(f"{URL}/key", json={"api_key": NEW_KEY}, headers=H).status_code == 200
    assert client.delete(f"{URL}/key", headers=H).status_code == 200
    assert not key_file.exists() and client.get(URL).json()["configured"] is False


def test_server_key_409(tmp_path):
    client, _ = _client(tmp_path, _helius(), server_key=True)
    data = client.get(URL)
    assert data.json()["key_source"] == "server" and data.json()["can_set_key"] is False
    _absent(data, SERVER_KEY)
    assert client.put(f"{URL}/key", json={"api_key": NEW_KEY}, headers=H).status_code == 409
    assert client.delete(f"{URL}/key", headers=H).status_code == 409


def test_key_route_requires_csrf(tmp_path):
    client, _ = _client(tmp_path, _helius())
    assert client.put(f"{URL}/key", json={"api_key": NEW_KEY}).status_code == 403
