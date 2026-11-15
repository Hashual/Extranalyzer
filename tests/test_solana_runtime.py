"""Configuration Helius et actualisation des portefeuilles Solana (faux Helius, données fictives)."""
import json
import logging
from datetime import datetime, timezone

import pytest

from app.config import SolanaSettings, _solana_settings
from app.sources.enablebanking.client import EnableBankingError
from app.sources.solana import wallets as w
from app.sources.solana.holdings import EURC_MINT
from app.sources.solana.runtime import RefreshBusy, RefreshSummary, SolanaRuntime
from app.store.db import connect
from tests.fakehelius import FakeHelius
from tests.test_solana_holdings import fungible

ADDR_A = "11111111111111111111111111111112"
ADDR_B = "11111111111111111111111111111113"
KEY = "cle-secrete-fictive"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _ok_fake():
    fake = FakeHelius()
    fake.on("getAssetsByOwner", {"items": [fungible(EURC_MINT, 0, 6, 1.08, "EURC")],
                                 "nativeBalance": {"lamports": 2_000_000_000, "price_per_sol": 150.0}})
    fake.on("getProgramAccounts", [])
    return fake


def _by_address(failing: dict[str, tuple[int, object]]):
    """Transport : l'adresse listée dans `failing` reçoit l'erreur HTTP donnée, les autres le faux serveur nominal."""
    ok = _ok_fake()

    def transport(method, url, headers, body):
        params = json.loads(body).get("params")
        owner = params.get("ownerAddress") if isinstance(params, dict) else None
        if owner in failing:
            status, content = failing[owner]
            return status, json.dumps(content).encode()
        return ok(method, url, headers, body)
    return transport


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    w.add_wallet(conn, "A", ADDR_A, "2026-01-01")
    w.add_wallet(conn, "B", ADDR_B, "2026-01-01")
    conn.commit()
    conn.close()
    return path


def _runner(path):
    def run(fn):
        conn = connect(path)
        try:
            return fn(conn)
        finally:
            conn.close()
    return run


def _runtime(db_path, transport):
    run = _runner(db_path)
    return SolanaRuntime(SolanaSettings(KEY), run, run, transport=transport, clock=lambda: NOW)


def _state(db_path, address):
    conn = connect(db_path)
    try:
        wid, last_error = conn.execute("SELECT id, last_error FROM wallets WHERE address = ?", (address,)).fetchone()
        statuses = [s for (s,) in conn.execute("SELECT status FROM wallet_snapshots WHERE wallet_id = ? ORDER BY id",
                                               (wid,))]
        return last_error, statuses
    finally:
        conn.close()


def test_settings_absent_key(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("EXTRANALYZER_HELIUS_KEY_FILE", str(tmp_path / "absent"))
    with caplog.at_level(logging.WARNING):
        assert _solana_settings() is None
    assert str(tmp_path) not in caplog.text and "FileNotFoundError" in caplog.text


def test_settings_empty_key(monkeypatch, tmp_path):
    (tmp_path / "k").write_text("\n")
    monkeypatch.setenv("EXTRANALYZER_HELIUS_KEY_FILE", str(tmp_path / "k"))
    assert _solana_settings() is None


def test_settings_read_key_and_url(monkeypatch, tmp_path):
    (tmp_path / "k").write_text(KEY + "\n")
    monkeypatch.setenv("EXTRANALYZER_HELIUS_KEY_FILE", str(tmp_path / "k"))
    monkeypatch.delenv("EXTRANALYZER_HELIUS_URL", raising=False)
    s = _solana_settings()
    assert s.api_key == KEY and s.base_url == "https://mainnet.helius-rpc.com"
    monkeypatch.setenv("EXTRANALYZER_HELIUS_URL", "https://exemple.test/rpc")
    assert _solana_settings().base_url == "https://exemple.test/rpc"


def test_settings_rejects_http_url(monkeypatch, tmp_path):
    (tmp_path / "k").write_text(KEY)
    monkeypatch.setenv("EXTRANALYZER_HELIUS_KEY_FILE", str(tmp_path / "k"))
    monkeypatch.setenv("EXTRANALYZER_HELIUS_URL", "http://exemple.test")
    assert _solana_settings() is None


def test_secret_never_in_repr():
    assert "cle" not in repr(SolanaSettings("cle-secrete-fictive"))


def test_refresh_one_failure_does_not_block_others(db_path):
    first = _runtime(db_path, _by_address({}))
    assert first.refresh("manuelle") == RefreshSummary(refreshed=2, errors=0)
    rt = _runtime(db_path, _by_address({ADDR_A: (500, {"error": {"code": -32000, "message": "panne"}})}))
    assert rt.refresh("manuelle") == RefreshSummary(refreshed=1, errors=1)
    last_error, statuses = _state(db_path, ADDR_A)
    assert statuses == ["ok", "error"] and last_error == "panne"
    last_error_b, statuses_b = _state(db_path, ADDR_B)
    assert statuses_b == ["ok", "ok"] and last_error_b is None


def test_refresh_single_wallet(db_path):
    rt = _runtime(db_path, _by_address({}))
    conn = connect(db_path)
    wid = conn.execute("SELECT id FROM wallets WHERE address = ?", (ADDR_B,)).fetchone()[0]
    conn.close()
    assert rt.refresh("manuelle", wallet_id=wid) == RefreshSummary(refreshed=1, errors=0)
    assert _state(db_path, ADDR_A)[1] == [] and _state(db_path, ADDR_B)[1] == ["ok"]


def test_refresh_rate_limited_message(db_path):
    rt = _runtime(db_path, _by_address({ADDR_A: (429, {"error": {"message": f"trop {KEY} api-key=x"}}),
                                        ADDR_B: (429, {})}))
    assert rt.refresh("manuelle") == RefreshSummary(refreshed=0, errors=2)
    last_error, _ = _state(db_path, ADDR_A)
    assert last_error and KEY not in last_error and "api-key" not in last_error
    assert _state(db_path, ADDR_B)[0]


def test_refresh_no_eur_rate_message(db_path):
    fake = FakeHelius()
    fake.on("getAssetsByOwner", {"items": [], "nativeBalance": {"lamports": 0}})
    fake.on("getAsset", {"id": EURC_MINT})
    rt = _runtime(db_path, fake)
    assert rt.refresh("manuelle") == RefreshSummary(refreshed=0, errors=2)
    assert _state(db_path, ADDR_A)[0] == "taux euro indisponible (prix de l'EURC absent)"


def test_refresh_unexpected_exception_is_contained(db_path, caplog):
    ok = _by_address({})

    def transport(method, url, headers, body):
        params = json.loads(body)["params"]
        if isinstance(params, dict) and params.get("ownerAddress") == ADDR_A:
            raise ValueError(f"boum {ADDR_A} {KEY}")
        return ok(method, url, headers, body)
    with caplog.at_level(logging.DEBUG):
        assert _runtime(db_path, transport).refresh("manuelle") == RefreshSummary(refreshed=1, errors=1)
    assert _state(db_path, ADDR_A)[0] == "réponse inattendue de Helius (ValueError)"
    assert _state(db_path, ADDR_B)[1] == ["ok"]
    assert ADDR_A not in caplog.text and KEY not in caplog.text


def test_refresh_busy(db_path):
    rt = _runtime(db_path, _by_address({}))
    assert rt._lock.acquire(blocking=False)
    try:
        with pytest.raises(RefreshBusy):
            rt.refresh("manuelle")
    finally:
        rt._lock.release()
    assert rt.refresh("manuelle").refreshed == 2


def test_scheduled_tick_never_raises(db_path):
    def boom(method, url, headers, body):
        raise EnableBankingError(0, "NETWORK", "coupé")
    _runtime(db_path, boom).scheduled_tick()
    assert _state(db_path, ADDR_A)[1] == ["error"]

    def crash(method, url, headers, body):
        raise RuntimeError("inattendu")
    _runtime(db_path, crash).scheduled_tick()


def test_scheduled_tick_unconfigured_and_busy(db_path):
    run = _runner(db_path)
    SolanaRuntime(None, run, run).scheduled_tick()
    assert not SolanaRuntime(None, run, run).configured
    rt = _runtime(db_path, _by_address({}))
    assert rt._lock.acquire(blocking=False)
    rt.scheduled_tick()
    rt._lock.release()
    assert _state(db_path, ADDR_A)[1] == []
