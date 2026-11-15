import pytest

from app.sources.enablebanking.client import EnableBankingError
from app.sources.solana.client import HeliusClient, HeliusError, RateLimited
from tests.fakehelius import FakeHelius

KEY = "cle-secrete-fictive"
OWNER = "ProprietaireFictif1111111111111111111111111"


def client(fake) -> HeliusClient:
    return HeliusClient(KEY, transport=fake)


def page(n: int, start: int = 0, **extra) -> dict:
    return {"total": n, "limit": 1000, "page": 1, "items": [{"id": f"mint{start + i}"} for i in range(n)], **extra}


def test_assets_paginates():
    fake = FakeHelius().on("getAssetsByOwner", page(1000), page(3, 1000))
    items, _ = client(fake).assets_by_owner(OWNER)
    assert len(items) == 1003
    calls = fake.calls("getAssetsByOwner")
    assert [c["params"]["page"] for c in calls] == [1, 2]
    assert calls[0]["params"]["ownerAddress"] == OWNER and calls[0]["params"]["limit"] == 1000
    assert calls[0]["params"]["displayOptions"] == {"showFungible": True, "showNativeBalance": True}
    assert calls[0]["http_method"] == "POST" and calls[0]["query"] == {"api-key": KEY}


def test_assets_native_balance():
    native = {"lamports": 5_000_000_000, "price_per_sol": 100.0, "total_price": 500.0}
    fake = FakeHelius().on("getAssetsByOwner", page(2, nativeBalance=native))
    items, balance = client(fake).assets_by_owner(OWNER)
    assert len(items) == 2 and balance == native


def test_assets_without_native_balance():
    _, balance = client(FakeHelius().on("getAssetsByOwner", page(1))).assets_by_owner(OWNER)
    assert balance is None


def test_asset():
    fake = FakeHelius().on("getAsset", {"id": "mintX", "content": {}})
    assert client(fake).asset("mintX")["id"] == "mintX"
    assert fake.calls("getAsset")[0]["params"]["id"] == "mintX"


def test_epoch():
    assert client(FakeHelius().on("getEpochInfo", {"epoch": 812, "slotIndex": 3})).epoch() == 812


def test_stake_accounts_dedup():
    a, b = {"pubkey": "stakeA", "account": {}}, {"pubkey": "stakeB", "account": {}}
    fake = FakeHelius().on("getProgramAccounts", [a, b], [b])
    found = client(fake).stake_accounts(OWNER)
    assert [s["pubkey"] for s in found] == ["stakeA", "stakeB"]
    calls = fake.calls("getProgramAccounts")
    assert len(calls) == 2
    assert [c["params"][0] for c in calls] == ["Stake11111111111111111111111111111111111111"] * 2
    for call, offset in zip(calls, (12, 44)):
        options = call["params"][1]
        assert options["encoding"] == "jsonParsed"
        assert options["filters"] == [{"memcmp": {"offset": offset, "bytes": OWNER}}]


def error_cases():
    def network(*_):
        raise EnableBankingError(0, "NETWORK", f"Enable Banking injoignable {KEY}")

    return [
        (lambda: FakeHelius().on("getEpochInfo", (429, {"error": {"message": f"too many {KEY}?api-key={KEY}"}})),
         RateLimited),
        (lambda: FakeHelius().on("getEpochInfo", (200, {"error": {"code": -32602, "message": f"x api-key {KEY}"}})),
         HeliusError),
        (lambda: FakeHelius().on("getEpochInfo", (500, {"message": f"boom {KEY}"})), HeliusError),
        (lambda: network, HeliusError),
    ]


def test_rate_limited():
    fake = FakeHelius().on("getEpochInfo", (429, {}))
    with pytest.raises(RateLimited) as exc:
        client(fake).epoch()
    assert exc.value.status == 429


def test_rpc_error():
    fake = FakeHelius().on("getEpochInfo", (200, {"error": {"code": -32602, "message": "x"}}))
    with pytest.raises(HeliusError) as exc:
        client(fake).epoch()
    assert not isinstance(exc.value, RateLimited) and exc.value.message


def test_http_error_message_truncated():
    fake = FakeHelius().on("getEpochInfo", (500, {"error": {"message": "y" * 500}}))
    with pytest.raises(HeliusError) as exc:
        client(fake).epoch()
    assert len(exc.value.message) <= 200


def test_network_error():
    def transport(*_):
        raise EnableBankingError(0, "NETWORK", "Enable Banking injoignable (URLError)")

    with pytest.raises(HeliusError) as exc:
        HeliusClient(KEY, transport=transport).epoch()
    assert exc.value.code == "NETWORK" and exc.value.status == 0
    assert exc.value.message == "Helius injoignable" and "Enable Banking" not in str(exc.value)


@pytest.mark.parametrize("make,expected", error_cases())
def test_errors_never_leak_key(make, expected):
    with pytest.raises(expected) as exc:
        HeliusClient(KEY, transport=make()).epoch()
    assert KEY not in str(exc.value) and "api-key" not in str(exc.value)
    assert KEY not in exc.value.message and "api-key" not in exc.value.message
