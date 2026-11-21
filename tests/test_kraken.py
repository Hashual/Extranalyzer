"""Compte Kraken : client signé, valorisation en euros, clé saisie dans l'interface, routes, Épargne (faux Kraken)."""
import json
import os
import stat
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.analytics import holdings as h
from app.analytics.periods import Period
from app.auth import hash_password
from app.config import Settings
from app.main import create_app
from app.sources.enablebanking.client import EnableBankingError
from app.sources.kraken.client import AuthError, KrakenClient, KrakenError, RateLimited
from app.sources.kraken.holdings import KNOWN_MINTS, read_account
from app.sources.kraken.runtime import KeyInvalid, KrakenRuntime, NotConfigured, account_id
from app.sources.solana import wallets as w
from app.sources.solana.holdings import SOL_MINT, Position, WalletReading
from app.store.db import connect
from tests.fakekraken import API_KEY, API_SECRET, FakeKraken
from tests.test_banque_wallets_api import ADDR_A, H, PASSWORD, _fake

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
URL = "/api/tabs/banque/kraken"
BALANCES = {"XXBT": "0.0100000000", "ZEUR": "100.5000", "SOL": "2.0", "SOL.S": "1.0", "DOT.S": "10",
            "USDC": "50.00", "KFEE": "500.00", "XETH": "0.0000000000", "OBSC": "3", "ETH2.S": "0.5"}


def _kraken(balance=None):
    return FakeKraken().on("Balance", BALANCES if balance is None else balance)


def _client(fake=None):
    return KrakenClient(API_KEY, API_SECRET, transport=fake or _kraken())


# --- client ----------------------------------------------------------------------------------------------------

def test_balance_is_signed_and_nonce_increases():
    fake = _kraken()
    client = _client(fake)
    assert client.balance()["XXBT"] == "0.0100000000"
    client.balance()
    first, second = fake.calls("Balance")
    assert first["http_method"] == "POST" and first["path"] == "/0/private/Balance"
    assert first["headers"]["API-Key"] == API_KEY
    assert int(second["form"]["nonce"]) > int(first["form"]["nonce"])


def test_wrong_secret_is_refused():
    fake = FakeKraken(secret="QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB").on("Balance", {})
    with pytest.raises(AuthError) as exc:
        _client(fake).balance()
    assert not exc.value.permission


@pytest.mark.parametrize("code,error,permission", [
    ("EAPI:Invalid key", AuthError, False),
    ("EGeneral:Permission denied", AuthError, True),
    ("EAPI:Rate limit exceeded", RateLimited, False),
    ("EGeneral:Internal error", KrakenError, False),
])
def test_errors_are_typed(code, error, permission):
    fake = FakeKraken().error("Balance", code)
    with pytest.raises(error) as exc:
        _client(fake).balance()
    assert type(exc.value) is error
    if error is AuthError:
        assert exc.value.permission is permission
    assert API_KEY not in exc.value.message and API_SECRET not in exc.value.message


def test_http_429_and_network_errors():
    with pytest.raises(RateLimited):
        _client(FakeKraken().on("Balance", (429, {}))).balance()

    def down(*_):
        raise EnableBankingError(0, "NETWORK", "injoignable")
    with pytest.raises(KrakenError) as exc:
        KrakenClient(API_KEY, API_SECRET, transport=down).balance()
    assert exc.value.message == "Kraken injoignable"


def test_secret_must_be_base64():
    with pytest.raises(ValueError):
        KrakenClient(API_KEY, "pas du base64 !")


# --- valorisation ----------------------------------------------------------------------------------------------

def test_read_account_values_in_euros():
    fake = _kraken()
    reading = read_account(_client(fake))
    by = {(p.kind, p.symbol): p for p in reading.positions}
    assert set(by) == {("token", "BTC"), ("token", "EUR"), ("sol", "SOL"), ("stake", "SOL"), ("stake", "DOT"),
                       ("token", "USDC"), ("stake", "ETH"), ("token", "OBSC")}
    assert by[("token", "BTC")].value_eur_cents == 60_000 and by[("token", "BTC")].mint == "BTC"
    assert by[("token", "EUR")].value_eur_cents == 10_050 and by[("token", "EUR")].name == "Euro (espèces)"
    assert by[("sol", "SOL")].mint == SOL_MINT and by[("sol", "SOL")].value_eur_cents == 30_000
    assert by[("stake", "SOL")].mint == SOL_MINT and by[("stake", "SOL")].value_eur_cents == 15_000
    assert by[("stake", "DOT")].price_eur == Decimal(5)            # 6 $ / 1,2 $ par euro
    assert by[("token", "USDC")].mint == KNOWN_MINTS["USDC"] and by[("token", "USDC")].value_eur_cents == 4_600
    assert by[("stake", "ETH")].value_eur_cents == 150_000         # ETH2.S valorisé comme l'ETH
    assert by[("token", "OBSC")].price_eur is None and by[("token", "OBSC")].value_eur_cents is None
    assert by[("token", "BTC")].price_source == "kraken"
    assert reading.total_eur_cents == 274_650 and reading.unpriced_count == 1
    (ticker,) = fake.calls("Ticker")
    assert ".d" not in ticker["query"]["pair"] and "XXBTZEUR" in ticker["query"]["pair"]


def test_empty_account_makes_no_public_call():
    fake = _kraken({"ZEUR": "0.0000", "KFEE": "12.0"})
    reading = read_account(_client(fake))
    assert reading == WalletReading(positions=[], total_eur_cents=0, unpriced_count=0)
    assert not fake.calls("Ticker") and not fake.calls("AssetPairs")


def test_unknown_asset_without_assets_entry_falls_back():
    fake = _kraken({"XLTC": "1.0"})
    (position,) = read_account(_client(fake)).positions
    assert position.symbol == "LTC" and position.value_eur_cents is None


# --- runtime ---------------------------------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    connect(path).close()
    return path


def _runners(path):
    def run(fn):
        conn = connect(path)
        try:
            return fn(conn)
        finally:
            conn.close()
    return run, run


def _runtime(db_path, fake, key_file):
    read, write = _runners(db_path)
    return KrakenRuntime(read, write, transport=fake, clock=lambda: NOW, key_file=key_file)


def test_set_key_checks_stores_and_creates_account(db_path, tmp_path):
    key_file = tmp_path / "kraken.key"
    runtime = _runtime(db_path, _kraken(), key_file)
    assert not runtime.configured and runtime.can_set_key
    runtime.set_key(f"  {API_KEY} ", API_SECRET[:20] + "\n" + API_SECRET[20:])      # clé privée collée en deux
    assert runtime.configured
    assert json.loads(key_file.read_text(encoding="utf-8")) == {"api_key": API_KEY, "api_secret": API_SECRET}
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert not (tmp_path / "kraken.key.tmp").exists()
    conn = connect(db_path)
    (wid, label, address, provider, cat) = conn.execute(
        "SELECT w.id, w.label, w.address, w.provider, c.name FROM wallets w "
        "JOIN categories c ON c.id = w.savings_category_id").fetchone()
    assert (label, address, provider, cat) == ("Kraken", "kraken", "kraken", "Kraken")
    assert account_id(conn) == wid
    # relue au démarrage suivant
    assert _runtime(db_path, _kraken(), key_file).configured


@pytest.mark.parametrize("key,secret,message", [
    ("", API_SECRET, "clé API"),
    ("court", API_SECRET, "clé API"),
    ("cle avec des espaces dedans", API_SECRET, "clé API"),
    (API_KEY, "pas-du-base64", "clé privée"),
    (API_KEY, "QUJD", "clé privée"),                 # base64 valide mais trop court
])
def test_set_key_rejects_malformed(db_path, tmp_path, key, secret, message):
    fake = _kraken()
    runtime = _runtime(db_path, fake, tmp_path / "kraken.key")
    with pytest.raises(KeyInvalid, match=message):
        runtime.set_key(key, secret)
    assert not fake.requests and not (tmp_path / "kraken.key").exists()


@pytest.mark.parametrize("code,message", [("EAPI:Invalid key", "refusée"),
                                          ("EGeneral:Permission denied", "Query Funds")])
def test_set_key_refused_by_kraken(db_path, tmp_path, code, message):
    runtime = _runtime(db_path, FakeKraken().error("Balance", code), tmp_path / "kraken.key")
    with pytest.raises(KeyInvalid, match=message):
        runtime.set_key(API_KEY, API_SECRET)
    assert not runtime.configured and not (tmp_path / "kraken.key").exists()
    assert connect(db_path).execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0


def test_refresh_records_snapshot_then_error(db_path, tmp_path):
    fake = _kraken()
    runtime = _runtime(db_path, fake, tmp_path / "kraken.key")
    runtime.set_key(API_KEY, API_SECRET)
    summary = runtime.refresh("manuelle")
    assert (summary.refreshed, summary.errors) == (1, 0)
    fake.error("Balance", "EAPI:Rate limit exceeded")
    summary = runtime.refresh("manuelle")
    assert (summary.refreshed, summary.errors) == (0, 1)
    conn = connect(db_path)
    data = w.overview(conn)
    (account,) = data["wallets"]
    assert account["provider"] == "kraken" and account["total_eur"] == Decimal("2746.50")
    assert account["last_error"] == "limite de requêtes Kraken atteinte, réessayez plus tard"


def test_refresh_without_key_and_clear_key(db_path, tmp_path):
    key_file = tmp_path / "kraken.key"
    runtime = _runtime(db_path, _kraken(), key_file)
    with pytest.raises(NotConfigured):
        runtime.refresh("manuelle")
    runtime.scheduled_tick()                         # sans clé : ne fait rien, n'échoue pas
    runtime.set_key(API_KEY, API_SECRET)
    runtime.refresh("manuelle")
    runtime.clear_key()
    assert not runtime.configured and not key_file.exists()
    assert connect(db_path).execute("SELECT COUNT(*) FROM wallet_snapshots").fetchone()[0] == 1   # historique gardé


def test_refresh_recreates_removed_account(db_path, tmp_path):
    runtime = _runtime(db_path, _kraken(), tmp_path / "kraken.key")
    runtime.set_key(API_KEY, API_SECRET)
    conn = connect(db_path)
    w.remove_wallet(conn, account_id(conn))
    conn.commit()
    assert runtime.refresh("manuelle").refreshed == 1
    assert account_id(connect(db_path)) is not None


def test_unreadable_stored_key_is_ignored(db_path, tmp_path):
    key_file = tmp_path / "kraken.key"
    key_file.write_text("pas du json", encoding="utf-8")
    assert not _runtime(db_path, _kraken(), key_file).configured


# --- routes ----------------------------------------------------------------------------------------------------

def _app(tmp_path, kraken):
    settings = Settings(data_dir=tmp_path / "data", password_hash=hash_password(PASSWORD), max_upload_mb=1)
    client = TestClient(create_app(settings, solana_transport=_fake(), kraken_transport=kraken))
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return client


def _absent(response):
    assert API_KEY not in response.text and API_SECRET not in response.text


def test_routes_link_account_and_list_holdings(tmp_path):
    client = _app(tmp_path, _kraken())
    assert client.get(URL).json() == {"configured": False, "can_set_key": True, "account_id": None}
    assert client.post(f"{URL}/refresh", headers=H).status_code == 503
    r = client.put(f"{URL}/key", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=H)
    assert r.status_code == 200 and r.json()["refresh"] == {"refreshed": 1, "errors": 0}
    _absent(r)
    status = client.get(URL).json()
    assert status["configured"] is True and status["account_id"] is not None
    data = client.get("/api/tabs/banque/wallets")
    _absent(data)
    (account,) = data.json()["wallets"]
    assert account["provider"] == "kraken" and account["label"] == "Kraken"
    btc = next(p for p in account["positions"] if p["symbol"] == "BTC")
    assert btc["amount"] == "0.0100000000" and btc["price_eur"] == "60000.0"
    assert client.post(f"{URL}/refresh", headers=H).json() == {"refreshed": 1, "errors": 0}
    assert client.delete(f"{URL}/key", headers=H).json() == {"ok": True}
    assert client.get(URL).json()["configured"] is False


def test_route_rejects_bad_key(tmp_path):
    client = _app(tmp_path, FakeKraken().error("Balance", "EGeneral:Permission denied"))
    r = client.put(f"{URL}/key", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=H)
    assert r.status_code == 422 and "Query Funds" in r.json()["detail"]
    r = client.put(f"{URL}/key", json={"api_key": "court", "api_secret": API_SECRET}, headers=H)
    assert r.status_code == 422
    _absent(r)


def test_route_kraken_down_is_502(tmp_path):
    client = _app(tmp_path, FakeKraken().error("Balance", "EService:Unavailable"))
    r = client.put(f"{URL}/key", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=H)
    assert r.status_code == 502


def test_solana_refresh_ignores_kraken_account(tmp_path):
    client = _app(tmp_path, _kraken())
    client.put(f"{URL}/key", json={"api_key": API_KEY, "api_secret": API_SECRET}, headers=H)
    helius = client.app.state.solana
    assert helius.configured is False                # pas de clé Helius : la relève Solana ne touche à rien
    client.app.state.solana.client = object()        # une relève Solana ne doit jamais lire le compte Kraken
    summary = helius.refresh("manuelle")
    assert (summary.refreshed, summary.errors) == (0, 0)


# --- Épargne ---------------------------------------------------------------------------------------------------

def test_epargne_merges_same_crypto_across_sources(tmp_path):
    conn = connect(tmp_path / "e.db")
    sol_wallet = w.add_wallet(conn, "Principal", ADDR_A, "2026-11-01")
    w.record_reading(conn, sol_wallet, "2026-11-20T10:00:00+00:00", WalletReading(
        positions=[Position("sol", Decimal(1), mint=SOL_MINT, symbol="SOL", value_eur_cents=15_000),
                   Position("stake", Decimal(2), symbol="SOL", value_eur_cents=30_000)],
        total_eur_cents=45_000, unpriced_count=0))
    from app.sources.kraken.runtime import ensure_account
    kraken = ensure_account(conn, "2026-11-01")
    reading = read_account(_client())
    w.record_reading(conn, kraken, "2026-11-20T11:00:00+00:00", reading)
    conn.commit()
    d = h.dashboard(conn, Period(date(2026, 11, 1), date(2026, 11, 30)), date(2026, 11, 21))
    assert d["crypto"]["value"] == 45_000 + 274_650 and d["crypto"]["count"] == 2
    assert {p["provider"] for p in d["places"] if p["kind"] == "wallet"} == {"solana", "kraken"}
    cats = {c["key"]: c for c in d["crypto_categories"]}
    assert cats["SOL"]["value"] == 45_000 + 30_000 + 15_000          # portefeuille + Kraken (disponible et placé)
    assert cats["ETH"]["value"] == 150_000 and cats["EUR"]["value"] == 10_050
    assert cats[KNOWN_MINTS["USDC"]]["name"] == "USDC"
    names = {s["name"] for s in d["allocation"]["by_asset"]}
    assert "ETH en staking" in names and "SOL en staking" in names
