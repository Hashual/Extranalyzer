from decimal import Decimal

import pytest

from app.sources.solana.client import HeliusClient
from app.sources.solana.holdings import EURC_MINT, NoEurRate, SOL_MINT, read_wallet, stake_state
from tests.fakehelius import FakeHelius

OWNER = "OwnerFictif1111111111111111111111111111111"
USDC = "UsdcFictif11111111111111111111111111111111"
XSTOCK = "XStockFictif111111111111111111111111111111"
NEVER = str(2**64 - 1)


def fungible(mint, balance, decimals, price, symbol="TKN", interface="FungibleToken"):
    token_info = {"balance": balance, "decimals": decimals, "symbol": symbol}
    if price is not None:
        token_info["price_info"] = {"price_per_token": price, "currency": "USDC"}
    return {"id": mint, "interface": interface, "content": {"metadata": {"name": f"Nom {symbol}", "symbol": symbol}},
            "token_info": token_info}


def stake(pubkey, lamports, activation="1", deactivation=NEVER, voter="VoteFictif1111111111111111111111111111111"):
    return {"pubkey": pubkey, "account": {"lamports": lamports, "data": {"parsed": {
        "type": "delegated", "info": {"stake": {"delegation": {
            "voter": voter, "activationEpoch": activation, "deactivationEpoch": deactivation}}}}}}}


def make(items=(), lamports=2_000_000_000, stakes=(), epoch=500, eurc=1.08, sol_price=150.0):
    fake = FakeHelius()
    items = list(items)
    if eurc is not None:
        items.append(fungible(EURC_MINT, 0, 6, eurc, "EURC"))
    native = {"lamports": lamports}
    if sol_price is not None:
        native["price_per_sol"] = sol_price
    fake.on("getAssetsByOwner", {"items": items, "nativeBalance": native})
    fake.on("getProgramAccounts", list(stakes), [])
    fake.on("getEpochInfo", {"epoch": epoch})
    return fake, HeliusClient("cle-fictive", transport=fake)


def cents(usd):
    return int((Decimal(str(usd)) / Decimal("1.08") * 100).quantize(Decimal(1)))


def test_reads_sol_tokens_and_stake():
    fake, client = make([fungible(USDC, 100_000_000, 6, 1.0, "USDC"),
                         fungible(XSTOCK, 500_000, 6, 300.0, "xAAPL", "FungibleAsset")],
                        stakes=[stake("Stake1", 10_000_000_000)])
    r = read_wallet(client, OWNER)
    kinds = sorted(p.kind for p in r.positions)
    assert kinds == ["sol", "stake", "token", "token"]
    assert len(r.positions) == 4
    assert r.total_eur_cents == cents(2 * 150 + 100 * 1 + Decimal("0.5") * 300 + 10 * 150)
    assert r.unpriced_count == 0
    tok = next(p for p in r.positions if p.mint == USDC)
    assert tok.amount == Decimal(100) and tok.symbol == "USDC" and tok.price_source == "helius"
    st = next(p for p in r.positions if p.kind == "stake")
    assert st.stake_account == "Stake1" and st.stake_state == "active" and st.amount == Decimal(10)


def test_nft_ignored():
    nft = {"id": "NftFictif", "interface": "V1_NFT", "content": {"metadata": {"name": "Image"}}}
    _, client = make([nft])
    r = read_wallet(client, OWNER)
    assert all(p.mint != "NftFictif" for p in r.positions)


def test_token_without_price():
    _, client = make([fungible(USDC, 5_000_000, 6, None, "USDC")])
    r = read_wallet(client, OWNER)
    p = next(p for p in r.positions if p.mint == USDC)
    assert p.price_eur is None and p.value_eur_cents is None
    assert r.unpriced_count == 1


def test_hidden_mint_excluded_from_totals():
    items = [fungible(USDC, 100_000_000, 6, 1.0, "USDC"), fungible(XSTOCK, 500_000, 6, None, "X")]
    _, client = make(items)
    r = read_wallet(client, OWNER, hidden_mints=frozenset({USDC, XSTOCK}))
    assert {p.mint for p in r.positions} >= {USDC, XSTOCK}
    assert r.total_eur_cents == cents(2 * 150)
    assert r.unpriced_count == 0


def test_empty_wallet():
    _, client = make(lamports=0)
    r = read_wallet(client, OWNER)
    assert r.positions == []
    assert r.total_eur_cents == 0 and r.unpriced_count == 0


def test_sol_price_falls_back_to_asset():
    fake, client = make(sol_price=None)
    fake.on("getAsset", {"id": SOL_MINT, "token_info": {"price_info": {"price_per_token": 150.0}}})
    r = read_wallet(client, OWNER)
    assert r.total_eur_cents == cents(2 * 150)


def test_eurc_price_from_asset_when_absent_from_items():
    fake, client = make(eurc=None)
    fake.on("getAsset", {"id": EURC_MINT, "token_info": {"price_info": {"price_per_token": 1.08}}})
    r = read_wallet(client, OWNER)
    assert r.total_eur_cents == cents(2 * 150)


def test_stake_states():
    assert stake_state(501, int(NEVER), 500) == "activating"
    assert stake_state(500, int(NEVER), 500) == "activating"
    assert stake_state(10, int(NEVER), 500) == "active"
    assert stake_state(10, 500, 500) == "deactivating"
    assert stake_state(10, 499, 500) == "inactive"


def test_inactive_states_still_valued():
    _, client = make(lamports=0, stakes=[
        stake("S1", 1_000_000_000, deactivation="500"),
        stake("S2", 2_000_000_000, deactivation="400"),
        {"pubkey": "S3", "account": {"lamports": 3_000_000_000, "data": {"parsed": {"type": "initialized", "info": {}}}}},
    ])
    r = read_wallet(client, OWNER)
    by = {p.stake_account: p for p in r.positions}
    assert by["S1"].stake_state == "deactivating"
    assert by["S2"].stake_state == "inactive"
    assert by["S3"].stake_state == "inactive" and by["S3"].vote_account is None
    assert r.total_eur_cents == cents(150) + cents(300) + cents(450)  # arrondi par position


def test_no_eurc_price_raises():
    _, client = make(eurc=None)
    client_fake = client._transport
    client_fake.on("getAsset", {"id": EURC_MINT, "token_info": {}})
    with pytest.raises(NoEurRate):
        read_wallet(client, OWNER)


def test_hiding_wsol_mint_keeps_native_sol():
    wsol = fungible(SOL_MINT, 1_000_000_000, 9, 150.0, "SOL")
    _, client = make([wsol])
    r = read_wallet(client, OWNER, hidden_mints=frozenset({SOL_MINT}))
    assert r.total_eur_cents == cents(2 * 150)      # le SOL natif reste compté, le jeton wSOL est masqué
    assert r.unpriced_count == 0
