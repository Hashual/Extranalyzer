"""Routes Gmail de l'onglet Connexions (faux IMAP, aucun réseau, aucune relève de fond)."""
import imaplib

import pytest
from fastapi.testclient import TestClient

import app.receipts.mailsync as mailsync
from app.auth import hash_password
from app.config import MailSettings, Settings
from app.main import create_app
from tests.test_mailsync import FakeImap

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
URL = "/api/tabs/banque/gmail"
ADDRESS = "lecteur.fictif@example.test"
APP_PASSWORD = "wxyz wxyz wxyz wxyz"
SECRET = "wxyzwxyzwxyzwxyz"
LABELS = (("Libellé fictif", "superu"),)


def _client(tmp_path, imap=None, server=False):
    mail = MailSettings(user="serveur@example.test", app_password="secret-serveur", labels=LABELS) if server else None
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1,
                        mail=mail, mail_labels=LABELS)
    app = create_app(settings, start_mail_thread=False, mail_connect=imap or FakeImap({"INBOX": (1, {})}))
    client = TestClient(app)
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return client


def _no_secret(response):
    assert SECRET not in response.text and "wxyz" not in response.text


def test_status_when_not_configured(tmp_path):
    client = _client(tmp_path)
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json() == {"source": None, "configured": False, "address": None, "labels": [
        {"label": "Libellé fictif", "store": "superu", "last_success_at": None, "last_attempt_at": None,
         "last_error": None}]}
    assert client.post(f"{URL}/sync", headers=H).status_code == 409
    assert client.get("/api/tabs/courses/sync").json()["enabled"] is False


def test_connect_status_sync_disconnect(tmp_path, monkeypatch):
    imap = FakeImap({"INBOX": (1, {})})
    client = _client(tmp_path, imap)
    r = client.put(f"{URL}/credentials", json={"address": ADDRESS, "app_password": APP_PASSWORD}, headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "address": "le…@example.test"}
    _no_secret(r)
    assert ("select", "INBOX", True) in imap.commands
    assert (tmp_path / "data" / "gmail.json").exists()
    status = client.get(URL)
    _no_secret(status)
    assert status.json()["source"] == "app" and status.json()["configured"] is True
    assert ADDRESS not in status.text and status.json()["address"] == "le…@example.test"
    assert client.get("/api/tabs/courses/sync").json()["enabled"] is True        # état vivant

    calls = []
    monkeypatch.setattr(mailsync, "sync_once",
                        lambda cfg, *a, **k: calls.append(cfg.user) or {"messages": 1, "files": 2, "error": None})
    sync = client.post(f"{URL}/sync", headers=H)
    assert sync.status_code == 200 and sync.json() == {"messages": 1, "files": 2, "error": None}
    assert client.post("/api/tabs/courses/sync", headers=H).status_code == 200 and calls == [ADDRESS, ADDRESS]

    gone = client.delete(f"{URL}/credentials", headers=H)
    assert gone.status_code == 200 and not (tmp_path / "data" / "gmail.json").exists()
    assert client.get(URL).json()["configured"] is False
    assert client.get("/api/tabs/courses/sync").json()["enabled"] is False
    assert client.post("/api/tabs/courses/sync", headers=H).status_code == 409


def test_saved_credentials_survive_restart(tmp_path):
    client = _client(tmp_path)
    client.put(f"{URL}/credentials", json={"address": ADDRESS, "app_password": APP_PASSWORD}, headers=H)
    again = _client(tmp_path)
    assert again.get(URL).json()["source"] == "app"


@pytest.mark.parametrize("imap, status", [
    (FakeImap({"INBOX": (1, {})}, login_error=imaplib.IMAP4.error(f"refus pour {SECRET}")), 422),
    (lambda *a, **k: (_ for _ in ()).throw(OSError("réseau")), 502),
])
def test_connect_refused_saves_nothing(tmp_path, imap, status):
    client = _client(tmp_path, imap)
    r = client.put(f"{URL}/credentials", json={"address": ADDRESS, "app_password": APP_PASSWORD}, headers=H)
    assert r.status_code == status and r.json()["detail"]
    _no_secret(r)
    assert not (tmp_path / "data" / "gmail.json").exists()
    assert client.get(URL).json()["configured"] is False


@pytest.mark.parametrize("body", [
    {"address": "pas-une-adresse", "app_password": APP_PASSWORD},
    {"app_password": APP_PASSWORD},
    {"address": ADDRESS, "app_password": ["wxyz"]},
    {"address": 12, "app_password": APP_PASSWORD},
    [APP_PASSWORD],
])
def test_bad_body_never_echoes_password(tmp_path, body):
    client = _client(tmp_path)
    r = client.put(f"{URL}/credentials", json=body, headers=H)
    assert r.status_code == 422
    _no_secret(r)


def test_server_managed_is_read_only(tmp_path):
    client = _client(tmp_path, server=True)
    status = client.get(URL).json()
    assert status["source"] == "server" and status["configured"] is True and status["address"] == "se…@example.test"
    assert "secret-serveur" not in client.get(URL).text
    put = client.put(f"{URL}/credentials", json={"address": ADDRESS, "app_password": APP_PASSWORD}, headers=H)
    assert put.status_code == 409
    assert client.delete(f"{URL}/credentials", headers=H).status_code == 409
    assert not (tmp_path / "data" / "gmail.json").exists()


def test_guard_requires_csrf_and_session(tmp_path):
    client = _client(tmp_path)
    body = {"address": ADDRESS, "app_password": APP_PASSWORD}
    assert client.put(f"{URL}/credentials", json=body).status_code == 403
    assert client.delete(f"{URL}/credentials").status_code == 403
    assert client.post(f"{URL}/sync").status_code == 403
    anonymous = TestClient(client.app)
    assert anonymous.get(URL).status_code == 401
    assert anonymous.put(f"{URL}/credentials", json=body, headers=H).status_code == 401
    assert not (tmp_path / "data" / "gmail.json").exists()


@pytest.mark.parametrize("body", [{"address": "léa.fictive@example.test", "app_password": APP_PASSWORD},
                                  {"address": ADDRESS, "app_password": "wxyz wxyz wxyz wxy’"}])
def test_non_ascii_credentials_refused_with_422(tmp_path, body):
    imap = FakeImap({"INBOX": (1, {})})
    client = _client(tmp_path, imap)
    r = client.put(f"{URL}/credentials", json=body, headers=H)
    assert r.status_code == 422 and r.json()["detail"]
    _no_secret(r)
    assert imap.commands == [] and not (tmp_path / "data" / "gmail.json").exists()


def test_oversized_body_refused(tmp_path):
    client = _client(tmp_path)
    big = {"address": ADDRESS, "app_password": APP_PASSWORD, "bourrage": "x" * 5000}
    r = client.put(f"{URL}/credentials", json=big, headers=H)
    assert r.status_code == 413
    _no_secret(r)
    chunks = (part for part in [b'{"address": "', b"x" * 5000, b'"}'])
    r = client.put(f"{URL}/credentials", content=chunks, headers=H)     # sans Content-Length (transfert par blocs)
    assert r.status_code == 411
    assert not (tmp_path / "data" / "gmail.json").exists()
