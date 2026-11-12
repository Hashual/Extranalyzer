"""Onglet Banque et routes de la connexion bancaire (faux serveur Enable Banking, données fictives)."""
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.config import BankSettings, Settings
from app.main import create_app
from app.store.db import connect
from fakebank import FakeBank, booked, rsa_key_pem

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
IBAN_CC = "FR7600000000000000000000142"
REDIRECT = "http://10.0.0.1:8000/bank/callback"


def _settings(tmp_path, bank=True):
    return Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1,
                    bank=BankSettings("app-fictive", rsa_key_pem(), REDIRECT) if bank else None)


def _login(client):
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200


@pytest.fixture
def fake():
    bank = FakeBank()
    bank.on("GET", "/aspsps", {"aspsps": [{"name": "Crédit Mutuel", "country": "FR",
                                           "maximum_consent_validity": 15552000}]})
    bank.on("POST", "/auth", {"url": "https://banque.example/auth", "authorization_id": "a"})
    bank.on("POST", "/sessions", {"session_id": "sess-1", "aspsp": {"name": "Crédit Mutuel", "country": "FR"},
                                  "access": {"valid_until": "2099-01-01T00:00:00+00:00"},
                                  "accounts": [{"uid": "u-cc", "account_id": {"iban": IBAN_CC}, "name": "Compte",
                                                "currency": "EUR", "cash_account_type": "CACC",
                                                "identification_hash": "H1"}]})
    bank.on("GET", "/accounts/u-cc/transactions", {"transactions": [booked("2025-03-03", "12.50", "CB FICTIVE")]})
    bank.on("GET", "/accounts/u-cc/balances", {"balances": []})
    bank.on("DELETE", "/sessions/sess-1", {"message": "OK"})
    return bank


@pytest.fixture
def env(tmp_path, fake):
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings, bank_transport=fake))
    _login(client)
    conn = connect(settings.data_dir / "extranalyzer.db")
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '00000000001', 'Courant fictif', 'checking')")
    conn.commit()
    yield client, conn, fake
    conn.close()


def _connect(client, fake):
    assert client.post("/api/tabs/banque/connect", headers=H).json() == {"url": "https://banque.example/auth"}
    return fake.calls("POST", "/auth")[-1]["body"]["state"]


def test_not_configured(tmp_path):
    client = TestClient(create_app(_settings(tmp_path, bank=False)))
    _login(client)
    data = client.get("/api/tabs/banque/status").json()
    assert data["configured"] is False and data["link"] is None
    assert client.post("/api/tabs/banque/connect", headers=H).status_code == 409
    assert client.post("/api/tabs/banque/sync", headers=H).status_code == 409
    assert client.get("/api/meta").json()["bank"] is None


def test_routes_require_session_and_csrf(tmp_path, fake):
    client = TestClient(create_app(_settings(tmp_path), bank_transport=fake))
    assert client.get("/api/tabs/banque/status").status_code == 401
    _login(client)
    assert client.post("/api/tabs/banque/connect").status_code == 403


def test_callback_page_served_without_session(tmp_path, fake):
    client = TestClient(create_app(_settings(tmp_path), bank_transport=fake))
    page = client.get("/bank/callback?code=c&state=s")
    assert page.status_code == 200 and "bank-callback.js" in page.text
    assert fake.requests == []                      # la page seule n'appelle rien


def test_full_connection_flow(env):
    client, conn, fake = env
    state = _connect(client, fake)
    summary = client.post("/api/tabs/banque/session", json={"code": "code-1", "state": state}, headers=H).json()
    assert summary["added"] == 1
    assert fake.calls("POST", "/sessions")[0]["body"] == {"code": "code-1"}
    data = client.get("/api/tabs/banque/status").json()
    assert data["link"]["status"] == "active"
    account = data["accounts"][0]
    assert (account["iban_tail"], account["account_id"], account["locked"]) == ("0142", 1, True)
    assert IBAN_CC not in str(data) and "sess-1" not in str(data)
    assert data["runs"][0]["trigger"] == "connexion"
    meta = client.get("/api/meta").json()
    assert meta["bank"]["status"] == "ok"
    # rejouer le même retour est refusé
    again = client.post("/api/tabs/banque/session", json={"code": "code-1", "state": state}, headers=H)
    assert again.status_code == 400


def test_pasted_return_url(env):
    client, _, fake = env
    state = _connect(client, fake)
    pasted = f"https://localhost/bank/callback?state={state}&code=code-2"
    assert client.post("/api/tabs/banque/session", json={"url": pasted}, headers=H).status_code == 200
    assert fake.calls("POST", "/sessions")[0]["body"] == {"code": "code-2"}


def test_bank_error_on_return_consumes_state(env):
    client, _, fake = env
    state = _connect(client, fake)
    refused = client.post("/api/tabs/banque/session",
                          json={"state": state, "error": "access_denied", "error_description": "refus"}, headers=H)
    assert refused.status_code == 400 and "refus" in refused.json()["detail"]
    assert fake.calls("POST", "/sessions") == []
    retry = client.post("/api/tabs/banque/session", json={"state": state, "code": "c"}, headers=H)
    assert retry.status_code == 400


def test_unknown_state_rejected(env):
    client, _, fake = env
    assert client.post("/api/tabs/banque/session", json={"code": "c", "state": "forge"}, headers=H).status_code == 400
    assert fake.calls("POST", "/sessions") == []


def test_manual_sync_and_upstream_error(env):
    client, _, fake = env
    client.post("/api/tabs/banque/session", json={"code": "c", "state": _connect(client, fake)}, headers=H)
    fake.on("GET", "/accounts/u-cc/transactions", {"transactions": [booked("2025-03-03", "12.50", "CB FICTIVE"),
                                                                    booked("2025-03-04", "1.00", "CB AUTRE")]})
    assert client.post("/api/tabs/banque/sync", headers=H).json()["added"] == 1
    fake.on("GET", "/aspsps", (503, {"message": "indisponible"}))
    failed = client.post("/api/tabs/banque/connect", headers=H)
    assert failed.status_code == 502 and "indisponible" in failed.json()["detail"]


def test_attach_ignore_and_accept_gap(env):
    client, conn, fake = env
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (2, '00000000002', 'Autre', 'checking')")
    conn.commit()
    fake.on("GET", "/accounts/u-cc/transactions", {"transactions": []})
    client.post("/api/tabs/banque/session", json={"code": "c", "state": _connect(client, fake)}, headers=H)
    assert client.post("/api/tabs/banque/accounts/1/attach", json={"account_id": 2}, headers=H).status_code == 200
    assert client.get("/api/tabs/banque/status").json()["accounts"][0]["account_id"] == 2
    assert client.post("/api/tabs/banque/accounts/1/attach", json={"ignore": True}, headers=H).status_code == 200
    assert client.get("/api/tabs/banque/status").json()["accounts"][0]["ignored"] is True
    assert client.post("/api/tabs/banque/accounts/99/attach", json={"account_id": 1}, headers=H).status_code == 404
    assert client.post("/api/tabs/banque/accounts/1/accept-gap", headers=H).status_code == 404   # aucun écart


def test_disconnect(env):
    client, _, fake = env
    client.post("/api/tabs/banque/session", json={"code": "c", "state": _connect(client, fake)}, headers=H)
    assert client.post("/api/tabs/banque/disconnect", headers=H).status_code == 200
    assert fake.calls("DELETE", "/sessions/sess-1")
    assert client.get("/api/tabs/banque/status").json()["link"] is None
    assert client.get("/api/meta").json()["bank"] is None


def test_scheduled_tick_syncs_and_never_raises(env):
    client, conn, fake = env
    client.post("/api/tabs/banque/session", json={"code": "c", "state": _connect(client, fake)}, headers=H)
    runtime = client.app.state.bank
    fake.on("GET", "/accounts/u-cc/transactions", {"transactions": [booked("2025-03-09", "3.00", "CB NOUVELLE")]})
    runtime.scheduled_tick()
    assert conn.execute("SELECT trigger, added FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone() == ("planifiée", 1)
    fake.on("GET", "/accounts/u-cc/transactions", (500, {"message": "panne"}))
    runtime.scheduled_tick()                        # l'erreur est enregistrée, rien n'est levé


def test_scheduler_runs_tick():
    import threading
    from app.sources.scheduler import Scheduler
    ticked = threading.Event()
    scheduler = Scheduler(ticked.set, every_s=60, first_delay_s=0.01)
    scheduler.start()
    try:
        assert ticked.wait(2)
    finally:
        scheduler.stop()


def test_return_waits_for_running_sync(env):
    import threading
    client, conn, fake = env
    runtime = client.app.state.bank
    state = _connect(client, fake)
    runtime._sync_lock.acquire()                    # une synchro planifiée est en cours
    threading.Timer(0.3, runtime._sync_lock.release).start()
    response = client.post("/api/tabs/banque/session", json={"code": "c", "state": state}, headers=H)
    assert response.status_code == 200 and response.json()["added"] == 1
