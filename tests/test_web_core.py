import os
import re
import sqlite3
import sys
import types

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.auth import RateLimiter, hash_password, verify_password
from app.config import ConfigError, Settings, load_settings
from app.main import create_app
from app.store.db import connect
from app.tabs import ENABLED_TABS, load_tabs
from app.tabs.registry import Tab
from app.web_json import mask_numbers
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


def test_load_settings_rejects_both_password_sources(tmp_path, monkeypatch):
    secret = tmp_path / "pw.txt"
    secret.write_text("x", encoding="utf-8")
    monkeypatch.setenv("EXTRANALYZER_PASSWORD", "x")
    monkeypatch.setenv("EXTRANALYZER_PASSWORD_FILE", str(secret))
    with pytest.raises(ConfigError):
        load_settings()


def test_load_settings_default_data_dir(monkeypatch):
    monkeypatch.delenv("EXTRANALYZER_DATA_DIR", raising=False)
    monkeypatch.delenv("EXTRANALYZER_MAX_UPLOAD_MB", raising=False)
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


def test_short_or_empty_secret_key_fails_closed(settings):
    settings.data_dir.mkdir(parents=True)
    key = settings.data_dir / "secret.key"
    key.write_bytes(b"")
    with pytest.raises(RuntimeError, match="secret.key"):
        create_app(settings)
    key.write_bytes(b"x" * 31)
    with pytest.raises(RuntimeError, match="secret.key"):
        create_app(settings)


def test_secret_key_created_without_leftover_temp_file(settings):
    _client(settings)
    assert sorted(p.name for p in settings.data_dir.iterdir() if "secret" in p.name) == ["secret.key"]


def test_security_headers(client):
    for path in ("/", "/static/app.js", "/api/tabs"):
        r = client.get(path)
        assert r.headers["content-security-policy"] ==             "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/tabs").headers["cache-control"] == "no-store"
    assert client.post("/api/logout").headers["cache-control"] == "no-store"      # réponse 403 du garde
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"     # revalidé, jamais périmé


def test_healthz_is_public_and_carries_security_headers(settings):
    anonymous = _client(settings)          # sans session ni en-tête CSRF
    r = anonymous.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in r.headers


def test_post_without_custom_header_forbidden(client):
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/reanalyse").status_code == 403
    assert client.post("/api/upload", files={"file": ("a.pdf", b"%PDF-1")}).status_code == 403
    assert client.post("/api/sections/1/resolve", json={"accept": False}).status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403
    assert client.get("/api/tabs").status_code == 200      # la session reste valide


# --- Registre d'onglets ----------------------------------------------------------------------

def test_default_tabs(client):
    assert ENABLED_TABS == ["overview", "categories", "courses", "transactions", "epargne", "a_classer", "releves",
                            "banque", "regles"]
    assert client.get("/api/tabs").json() == [
        {"id": "overview", "title": "Vue d'ensemble", "order": 10},
        {"id": "categories", "title": "Catégories", "order": 20},
        {"id": "courses", "title": "Courses", "order": 25},
        {"id": "transactions", "title": "Transactions", "order": 30},
        {"id": "epargne", "title": "Épargne", "order": 40},
        {"id": "a_classer", "title": "À classer", "order": 50},
        {"id": "releves", "title": "Relevés", "order": 60},
        {"id": "banque", "title": "Connexions", "order": 65},
        {"id": "regles", "title": "Règles", "order": 70},
    ]


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
    assert section["number_tail"] == "0001" and "account_number" not in section
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


def test_meta_attaches_deeper_categories_to_their_root(client, settings):
    conn = connect(settings.data_dir / "extranalyzer.db")
    with conn:
        root = conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES ('Racine test', NULL, "
                            "'expense')").lastrowid
        child = conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES ('Enfant', ?, 'expense')",
                             (root,)).lastrowid
        grandchild = conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES ('Petit-enfant', ?, "
                                  "'expense')", (child,)).lastrowid
    conn.close()
    r = client.get("/api/meta")
    assert r.status_code == 200
    [node] = [c for c in r.json()["categories"] if c["id"] == root]
    assert {c["id"] for c in node["children"]} == {child, grandchild}


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


def test_upload_without_content_length(client):
    r = client.post("/api/upload", content=iter([b"%PDF-1"]), headers={**H, "Content-Type": "multipart/form-data"})
    assert r.status_code == 411


def test_resolve_unknown_section(client):
    r = client.post("/api/sections/9999/resolve", json={"accept": True}, headers=H)
    assert r.status_code == 404


def test_account_numbers_masked_in_messages(client, tmp_path):
    client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))}, headers=H)
    # même opérations, autre date de relevé : collision d'identité → message citant le compte
    other = make_statement(tmp_path / "c.pdf", "10 mai 2024", [FSection(
        CC, ("CREDITEUR", "11/03/2024", "100,00"), ("CREDITEUR", "10/05/2024", "1.087,50"),
        _cc_section().txs)]).read_bytes()
    report = client.post("/api/upload", files={"file": ("c.pdf", other)}, headers=H)
    assert "00000000001" not in report.text
    section = report.json()["sections"][0]
    assert section["status"] == "error" and "0001" in section["message"]

    r = client.post(f"/api/sections/{section['section_id']}/resolve", json={"accept": True}, headers=H)
    assert r.status_code == 409


def test_mask_numbers():
    assert mask_numbers("compte 00000000001 au 2024-04-10, écart 1.087,50") ==         "compte ••0001 au 2024-04-10, écart 1.087,50"


def test_resolve_conflict_accept(client, tmp_path):
    client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))}, headers=H)
    changed = _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")])
    report = client.post("/api/upload", files={"file": ("b.pdf", changed)}, headers=H).json()
    section = report["sections"][0]
    assert section["status"] == "conflict"

    r = client.post(f"/api/sections/{section['section_id']}/resolve", json={"accept": True}, headers=H)
    assert r.status_code == 200 and r.json() == {"id": section["section_id"], "status": "ok", "orphaned_overrides": 0}
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
    assert report["sections"][0]["orphaned_overrides"] == 0


def test_delete_document_route(client, settings, tmp_path):
    data = _pdf(tmp_path, "a.pdf", [_cc_section()])
    document_id = client.post("/api/upload", files={"file": ("a.pdf", data)}, headers=H).json()["document_id"]
    assert client.delete(f"/api/documents/{document_id}").status_code == 403       # en-tête anti-CSRF requis

    r = client.delete(f"/api/documents/{document_id}", headers=H)

    assert r.status_code == 200
    assert r.json() == {"document_id": document_id, "deleted_transactions": 3, "reclassified_mirrors": 0,
                        "pending_versions": []}
    assert client.get("/api/tabs/releves/list").json()["documents"] == []
    assert client.get("/api/meta").json()["unclassified_count"] == 0
    assert list((settings.data_dir / "pdfs").iterdir()) == []
    # le même PDF se réimporte (dédoublonnage par sha256 du fichier)
    again = client.post("/api/upload", files={"file": ("a.pdf", data)}, headers=H).json()
    assert again["duplicate_file"] is False and again["sections"][0]["status"] == "ok"


def test_delete_unknown_document(client):
    r = client.delete("/api/documents/9999", headers=H)
    assert r.status_code == 404 and r.json()["detail"] == "relevé 9999 introuvable"


def test_delete_retained_version_reports_pending_versions(client, tmp_path):
    first = client.post("/api/upload", files={"file": ("a.pdf", _pdf(tmp_path, "a.pdf", [_cc_section()]))},
                        headers=H).json()
    changed = _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")])
    section_id = client.post("/api/upload", files={"file": ("b.pdf", changed)}, headers=H).json()["sections"][0]["section_id"]
    assert client.get("/api/tabs/releves/list").json()["documents"][0]["sections"][0]["other_ok"] is True

    r = client.delete(f"/api/documents/{first['document_id']}", headers=H)

    assert "00000000001" not in r.text
    assert r.json()["pending_versions"] == [{"section_id": section_id, "number_tail": "0001",
                                              "statement_date": "2024-04-10", "previous_status": "conflict"}]
    [doc] = client.get("/api/tabs/releves/list").json()["documents"]
    assert (doc["sections"][0]["status"], doc["sections"][0]["other_ok"]) == ("conflict", False)


# --- Statique --------------------------------------------------------------------------------

def test_static_shell_is_served(settings):
    c = _client(settings)
    r = c.get("/")
    assert r.status_code == 200 and "/static/app.js" in r.text
    assert "/static/vendor/chart.umd.min.js" in r.text
    assert re.search(r"<input id=.upload-input.[^>]* multiple[ >]", r.text)     # dépôt de plusieurs relevés
    for path in ("/static/app.js", "/static/api.js", "/static/style.css", "/static/tabs/overview.js", "/static/tabs/courses.js",
                 "/static/vendor/chart.umd.min.js"):
        assert c.get(path).status_code == 200, path


def test_courses_tab_module_is_served(settings):
    js = _client(settings).get("/static/tabs/courses.js").text
    assert 'export const id = "courses"' in js and 'export const title = "Courses"' in js
    assert "export async function render(" in js


def _rule_patterns(settings):
    conn = connect(settings.data_dir / "extranalyzer.db")
    try:
        return sorted(r[0] for r in conn.execute("SELECT pattern FROM rules"))
    finally:
        conn.close()


def test_seed_rules_only_on_fresh_install(settings):
    create_app(settings)
    assert _rule_patterns(settings) == ["F COTIS", "INTERETS"]
    conn = connect(settings.data_dir / "extranalyzer.db")
    conn.execute("DELETE FROM rules WHERE pattern = 'INTERETS'")
    conn.commit()
    conn.close()
    create_app(settings)                     # redémarrage : la règle supprimée ne revient pas
    assert _rule_patterns(settings) == ["F COTIS"]


def test_seed_rules_not_restored_when_transactions_exist(settings):
    create_app(settings)
    conn = connect(settings.data_dir / "extranalyzer.db")
    conn.execute("DELETE FROM rules")
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, 'A1', 'CC', 'checking')")
    conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents) "
                 "VALUES (1, 'k1', '2025-01-05', 'OP', 'op', -100)")
    conn.execute("DELETE FROM categories WHERE name = 'CEL'")
    conn.commit()
    conn.close()
    create_app(settings)
    assert _rule_patterns(settings) == []
    conn = connect(settings.data_dir / "extranalyzer.db")
    assert conn.execute("SELECT COUNT(*) FROM categories WHERE name = 'CEL'").fetchone()[0] == 1   # catégories : idempotent
    conn.close()


def test_startup_reclassifies_an_upgraded_database(settings):
    """Base v2 (avant le classement automatique) : la source d'un virement apparié mais non classée est classée
    'pair' dès le démarrage, sans attendre un import."""
    from importlib import resources
    from test_store import _strip_v3_columns
    schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
    settings.data_dir.mkdir(parents=True)
    raw = sqlite3.connect(settings.data_dir / "extranalyzer.db")
    raw.executescript(_strip_v3_columns(schema).replace("user_version = 4", "user_version = 2"))
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    raw.execute("INSERT INTO accounts(id, number, label, kind) VALUES (2, '2', 'LIVRET BLEU', 'savings')")
    cols = "(id, account_id, tx_key, op_date, label, label_norm, amount_cents, mirror_of)"
    raw.execute(f"INSERT INTO transactions{cols} VALUES (1, 1, 'k1', '2024-03-12', 'OP FICTIVE', 'OP FICTIVE', "
                "-10000, NULL)")
    raw.execute(f"INSERT INTO transactions{cols} VALUES (2, 2, 'k2', '2024-03-12', 'VERSEMENT', 'VERSEMENT', "
                "10000, 1)")
    raw.commit()
    raw.close()
    create_app(settings)
    conn = sqlite3.connect(settings.data_dir / "extranalyzer.db")
    try:
        rows = conn.execute("SELECT t.id, t.classified_by, c.name FROM transactions t "
                            "LEFT JOIN categories c ON c.id = t.category_id ORDER BY t.id").fetchall()
    finally:
        conn.close()
    assert rows == [(1, "pair", "Livret Bleu"), (2, "pair", "Livret Bleu")]


def test_courses_tab_has_two_step_delete_without_dialog(settings):
    js = _client(settings).get("/static/tabs/courses.js").text
    assert "Supprimer ce ticket" in js and "Confirmer la suppression" in js and "Annuler" in js
    assert "ctx.api.delete(" in js and "window.confirm" not in js and "alert(" not in js
