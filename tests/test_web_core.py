import os
import sys
import types

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.auth import RateLimiter, hash_password, verify_password
from app.config import ConfigError, Settings, load_settings
from app.main import create_app
from app.tabs import ENABLED_TABS, load_tabs
from app.tabs.registry import Tab
from pdfgen import FSection, FTx, make_statement

PASSWORD = "mot-de-passe-test"
H = {"X-Extranalyzer": "1"}
CC = "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"


def _cc_section(closing="1.087,50", vir="1.000,00"):
    return FSection(CC, ("CREDITEUR", "11/03/2024", "100,00"), ("CREDITEUR", "10/04/2024", closing), [
        FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 VILLEA", "MAGASIN ALPHA CARTE 0000"], debit="2,50"),
        FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit=vir),
        FTx("13/03/2024", "13/03/2024", ["PRLV SEPA BETA"], debit="10,00")])


def _pdf(tmp_path, name, sections):
    return make_statement(tmp_path / name, "10 avril 2024", sections).read_bytes()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)


def _fake_tab(monkeypatch, tab_id, order, title="Factice"):
    router = APIRouter()

    @router.get("/ping")
    def ping():
        return {"pong": tab_id}

    module = types.ModuleType(f"app.tabs.{tab_id}")
    module.TAB = Tab(tab_id, title, order, router)
    monkeypatch.setitem(sys.modules, f"app.tabs.{tab_id}", module)


def _client(settings, enabled_tabs=None):
    return TestClient(create_app(settings, enabled_tabs))


def _login(client):
    r = client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200
    return r


@pytest.fixture
def client(settings):
    c = _client(settings)
    _login(c)
    return c


# --- Configuration ---------------------------------------------------------------------------

def test_password_hash_roundtrip():
    stored = hash_password("abc")
    assert verify_password("abc", stored)
    assert not verify_password("abd", stored)
    assert hash_password("abc") != stored          # sel aléatoire


def test_load_settings_from_env(tmp_path, monkeypatch):
    for name in ("EXTRANALYZER_PASSWORD", "EXTRANALYZER_PASSWORD_FILE", "EXTRANALYZER_MAX_UPLOAD_MB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXTRANALYZER_DATA_DIR", str(tmp_path))
    with pytest.raises(ConfigError):
        load_settings()

    secret = tmp_path / "pw.txt"
    secret.write_text("depuis-fichier\n", encoding="utf-8")
    monkeypatch.setenv("EXTRANALYZER_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("EXTRANALYZER_MAX_UPLOAD_MB", "5")
    s = load_settings()
    assert s.data_dir == tmp_path and s.max_upload_mb == 5
    assert verify_password("depuis-fichier", s.password_hash)

    monkeypatch.delenv("EXTRANALYZER_PASSWORD_FILE")
    monkeypatch.delenv("EXTRANALYZER_MAX_UPLOAD_MB")
    monkeypatch.setenv("EXTRANALYZER_PASSWORD", "depuis-env")
    s = load_settings()
    assert s.max_upload_mb == 20 and verify_password("depuis-env", s.password_hash)


def test_load_settings_default_data_dir(monkeypatch):
    monkeypatch.delenv("EXTRANALYZER_DATA_DIR", raising=False)
    monkeypatch.delenv("EXTRANALYZER_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("EXTRANALYZER_PASSWORD", "x")
    assert str(load_settings().data_dir).replace("\\", "/") == "/data"


# --- Authentification ------------------------------------------------------------------------

def test_api_requires_session(settings):
    c = _client(settings)
    assert c.get("/api/tabs").status_code == 401
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/inexistant").status_code == 401


def test_login_sets_strict_httponly_cookie(settings):
    r = _login(_client(settings))
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("xa_session=")
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_wrong_password_rate_limited(settings):
    c = _client(settings)
    for _ in range(5):
        assert c.post("/api/login", json={"password": "faux"}, headers=H).status_code == 401
    assert c.post("/api/login", json={"password": "faux"}, headers=H).status_code == 429
    assert c.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 429


def test_rate_limiter_rolling_window():
    now = [1000.0]
    limiter = RateLimiter(limit=5, window=60, clock=lambda: now[0])
    limiter.acquire("1.2.3.4")
    limiter.succeeded("1.2.3.4")               # une réussite n'est pas comptée
    for _ in range(5):
        assert limiter.acquire("1.2.3.4")
    assert not limiter.acquire("1.2.3.4")
    assert limiter.acquire("5.6.7.8")
    now[0] += 61
    assert limiter.acquire("1.2.3.4")


def test_tampered_cookie_rejected(settings):
    c = _client(settings)
    c.cookies.set("xa_session", "falsifie.abc.def")
    assert c.get("/api/tabs").status_code == 401


def test_logout(client):
    assert client.post("/api/logout", headers=H).status_code == 200
    assert client.get("/api/tabs").status_code == 401


def test_session_survives_restart_with_same_secret(settings):
    c1 = _client(settings)
    _login(c1)
    key = settings.data_dir / "secret.key"
    assert key.is_file() and len(key.read_bytes()) >= 32
    if os.name == "posix":
        assert key.stat().st_mode & 0o777 == 0o600
    c2 = _client(settings)
    c2.cookies.set("xa_session", c1.cookies.get("xa_session"))
    assert c2.get("/api/tabs").status_code == 200


def test_post_without_custom_header_forbidden(client):
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/reanalyse").status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403
    assert client.get("/api/tabs").status_code == 200      # la session reste valide


# --- Registre d'onglets ----------------------------------------------------------------------

def test_default_tabs(client):
    assert ENABLED_TABS == ["overview"]
    assert client.get("/api/tabs").json() == [{"id": "overview", "title": "Vue d'ensemble", "order": 10}]


def test_tabs_listed_in_order(settings, monkeypatch):
    _fake_tab(monkeypatch, "fake_late", 99, "Tard")
    _fake_tab(monkeypatch, "fake_early", 1, "Tôt")
    c = _client(settings, ["fake_late", "overview", "fake_early"])
    _login(c)
    assert [t["id"] for t in c.get("/api/tabs").json()] == ["fake_early", "overview", "fake_late"]


def test_only_enabled_tabs_are_mounted(settings, monkeypatch):
    _fake_tab(monkeypatch, "fake", 50)
    c = _client(settings, ["overview"])
    _login(c)
    assert [t["id"] for t in c.get("/api/tabs").json()] == ["overview"]
    assert c.get("/api/tabs/fake/ping").status_code == 404


def test_injected_tab_module_is_served(settings, monkeypatch):
    _fake_tab(monkeypatch, "fake", 50)
    c = _client(settings, ["fake"])
    assert c.get("/api/tabs/fake/ping").status_code == 401
    _login(c)
    assert c.get("/api/tabs").json() == [{"id": "fake", "title": "Factice", "order": 50}]
    assert c.get("/api/tabs/fake/ping").json() == {"pong": "fake"}


def test_listed_missing_tab_fails_at_startup(settings):
    with pytest.raises(RuntimeError, match="absent"):
        create_app(settings, ["overview", "absent"])


def test_load_tabs_checks_tab_id(monkeypatch):
    _fake_tab(monkeypatch, "fake", 50)
    sys.modules["app.tabs.fake"].TAB = Tab("autre", "X", 1, APIRouter())
    with pytest.raises(RuntimeError, match="fake"):
        load_tabs(["fake"])


# --- Dépôt, méta, conflits, ré-analyse -------------------------------------------------------

def test_upload_synthetic_pdf_then_meta(client, tmp_path):
    files = {"file": ("releve.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]), "application/pdf")}
    r = client.post("/api/upload", files=files, headers=H)
    assert r.status_code == 200
    report = r.json()
    assert report["error"] is None and report["duplicate_file"] is False
    [section] = report["sections"]
    assert section["status"] == "ok" and section["tx_count"] == 3
    assert section["gap"] == "0.00" and section["statement_date"] == "2024-04-10"

    meta = client.get("/api/meta").json()
    [account] = meta["accounts"]
    assert account["number_tail"] == "0001" and account["kind"] == "checking"
    assert "00000000001" not in str(meta)
    assert set(account) == {"id", "label", "kind", "number_tail"}
    assert meta["data_range"] == {"min": "2024-03-11", "max": "2024-03-13"}
    assert isinstance(meta["unclassified_count"], int)
    assert meta["categories"] and {"id", "name", "kind", "children"} <= set(meta["categories"][0])

    again = client.post("/api/upload", files=files, headers=H).json()
    assert again["duplicate_file"] is True


def test_meta_empty_database(client):
    meta = client.get("/api/meta").json()
    assert meta["accounts"] == [] and meta["unclassified_count"] == 0
    assert meta["data_range"] == {"min": None, "max": None}


def test_upload_garbage_reports_error_and_server_survives(client, tmp_path):
    r = client.post("/api/upload", files={"file": ("x.pdf", b"hello", "application/pdf")}, headers=H)
    assert r.status_code == 200
    assert r.json()["error"]
    assert client.get("/api/meta").status_code == 200
    files = {"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]), "application/pdf")}
    assert client.post("/api/upload", files=files, headers=H).json()["sections"][0]["status"] == "ok"


def test_upload_over_limit(client):
    just_over = b"%PDF-" + b"0" * (1024 * 1024)
    r = client.post("/api/upload", files={"file": ("big.pdf", just_over, "application/pdf")}, headers=H)
    assert r.status_code == 413
    way_over = b"%PDF-" + b"0" * (3 * 1024 * 1024)
    r = client.post("/api/upload", files={"file": ("big.pdf", way_over, "application/pdf")}, headers=H)
    assert r.status_code == 413
    assert client.get("/api/meta").status_code == 200


def test_resolve_conflict_accept(client, tmp_path):
    client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))}, headers=H)
    changed = _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")])
    report = client.post("/api/upload", files={"file": ("b.pdf", changed)}, headers=H).json()
    section = report["sections"][0]
    assert section["status"] == "conflict"

    r = client.post(f"/api/sections/{section['section_id']}/resolve", json={"accept": True}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    r = client.post(f"/api/sections/{section['section_id']}/resolve", json={"accept": False}, headers=H)
    assert r.status_code == 409


def test_resolve_conflict_refuse(client, tmp_path):
    client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))}, headers=H)
    changed = _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")])
    section_id = client.post("/api/upload", files={"file": ("b.pdf", changed)}, headers=H).json()["sections"][0]["section_id"]
    r = client.post(f"/api/sections/{section_id}/resolve", json={"accept": False}, headers=H)
    assert r.json()["status"] == "superseded"


def test_reanalyse(client, tmp_path):
    client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))}, headers=H)
    r = client.post("/api/reanalyse", headers=H)
    assert r.status_code == 200
    [report] = r.json()
    assert report["error"] is None and report["sections"][0]["status"] == "ok"


# --- Statique --------------------------------------------------------------------------------

def test_static_shell_is_served(settings):
    c = _client(settings)
    r = c.get("/")
    assert r.status_code == 200 and "/static/app.js" in r.text
    assert "/static/vendor/chart.umd.min.js" in r.text
    for path in ("/static/app.js", "/static/api.js", "/static/style.css", "/static/tabs/overview.js",
                 "/static/vendor/chart.umd.min.js"):
        assert c.get(path).status_code == 200, path
