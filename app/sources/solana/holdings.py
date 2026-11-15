"""Lecture d'un portefeuille Solana en positions valorisées en euros (SOL, jetons, comptes de staking).

Le taux EUR vient du prix de l'EURC en dollars : eur_par_usd = 1 / prix_usd(EURC). Aucun prix n'est inventé :
une position sans prix reste sans valeur et est comptée dans `unpriced_count`.
"""
from dataclasses import dataclass
from decimal import Decimal

from app.importer.amounts import to_cents
from app.sources.solana.client import HeliusClient

SOL_MINT = "So11111111111111111111111111111111111111112"
EURC_MINT = "HzwqbKZw8HxMN6bF2yFZNrht3c2iXXzpKcFu7uBEDKtr"  # adresse Circle de l'EURC sur Solana
FUNGIBLE_INTERFACES = {"FungibleToken", "FungibleAsset"}
LAMPORTS_PER_SOL = Decimal(10) ** 9
EPOCH_NEVER = 2**64 - 1


class NoEurRate(Exception):
    """Le prix de l'EURC est introuvable : impossible de convertir les dollars en euros."""


@dataclass(frozen=True)
class Position:
    kind: str  # 'sol' | 'token' | 'stake'
    amount: Decimal
    mint: str | None = None
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    price_eur: Decimal | None = None
    price_source: str | None = None  # 'helius'
    value_eur_cents: int | None = None
    stake_account: str | None = None
    vote_account: str | None = None
    stake_state: str | None = None


@dataclass(frozen=True)
class WalletReading:
    positions: list[Position]
    total_eur_cents: int  # somme des positions valorisées et non masquées
    unpriced_count: int  # positions sans prix et non masquées


def stake_state(activation_epoch: int, deactivation_epoch: int, epoch: int) -> str:
    if activation_epoch >= epoch:
        return "activating"
    if deactivation_epoch == EPOCH_NEVER:
        return "active"
    if deactivation_epoch >= epoch:
        return "deactivating"
    return "inactive"


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def _usd_price(asset: dict) -> Decimal | None:
    info = (asset.get("token_info") or {}).get("price_info") or {}
    price = _decimal(info.get("price_per_token"))
    return price if price is not None and price > 0 else None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _epoch(value) -> int:
    return int(value)


def _stake_position(entry: dict, epoch: int, price_usd: Decimal | None, eur_per_usd: Decimal) -> Position | None:
    account = entry.get("account") or {}
    lamports = _decimal(account.get("lamports"))
    if lamports is None or lamports <= 0:
        return None
    parsed = ((account.get("data") or {}).get("parsed")) or {}
    delegation = (((parsed.get("info") or {}).get("stake")) or {}).get("delegation")
    if isinstance(delegation, dict):
        state = stake_state(_epoch(delegation.get("activationEpoch")), _epoch(delegation.get("deactivationEpoch")), epoch)
        vote = _text(delegation.get("voter"))
    else:
        state, vote = "inactive", None
    return _priced(Position("stake", lamports / LAMPORTS_PER_SOL, symbol="SOL", name="Solana",
                            decimals=9, stake_account=_text(entry.get("pubkey")), vote_account=vote,
                            stake_state=state), price_usd, eur_per_usd)


def _priced(position: Position, price_usd: Decimal | None, eur_per_usd: Decimal) -> Position:
    if price_usd is None:
        return position
    price_eur = price_usd * eur_per_usd
    return Position(**{**position.__dict__, "price_eur": price_eur, "price_source": "helius",
                       "value_eur_cents": to_cents(position.amount * price_eur)})


def _token_position(item: dict, price_eur_rate: Decimal) -> Position | None:
    info = item.get("token_info") or {}
    balance, decimals = _decimal(info.get("balance")), info.get("decimals")
    mint = _text(item.get("id"))
    if balance is None or not isinstance(decimals, int) or mint is None:
        return None
    amount = balance / (Decimal(10) ** decimals)
    if amount <= 0:
        return None
    meta = (item.get("content") or {}).get("metadata") or {}
    position = Position("token", amount, mint=mint, symbol=_text(info.get("symbol")) or _text(meta.get("symbol")),
                        name=_text(meta.get("name")), decimals=decimals)
    return _priced(position, _usd_price(item), price_eur_rate)


def read_wallet(client: HeliusClient, address: str, hidden_mints: frozenset[str] = frozenset()) -> WalletReading:
    items, native = client.assets_by_owner(address)
    fungibles = [i for i in items if i.get("interface") in FUNGIBLE_INTERFACES]

    eurc = next((i for i in fungibles if i.get("id") == EURC_MINT), None)
    eurc_usd = _usd_price(eurc) if eurc else None
    if eurc_usd is None:
        eurc_usd = _usd_price(client.asset(EURC_MINT))
    if eurc_usd is None:
        raise NoEurRate("prix de l'EURC introuvable")
    eur_per_usd = 1 / eurc_usd

    lamports = _decimal((native or {}).get("lamports")) or Decimal(0)
    sol_usd = _decimal((native or {}).get("price_per_sol"))
    if sol_usd is not None and sol_usd <= 0:
        sol_usd = None
    stakes = client.stake_accounts(address)
    if sol_usd is None and (lamports > 0 or stakes):
        sol_usd = _usd_price(client.asset(SOL_MINT))

    positions: list[Position] = []
    if lamports > 0:
        positions.append(_priced(Position("sol", lamports / LAMPORTS_PER_SOL, mint=SOL_MINT, symbol="SOL",
                                          name="Solana", decimals=9), sol_usd, eur_per_usd))
    positions += [p for p in (_token_position(i, eur_per_usd) for i in fungibles) if p is not None]
    if stakes:
        epoch = client.epoch()
        positions += [p for p in (_stake_position(e, epoch, sol_usd, eur_per_usd) for e in stakes) if p is not None]

    counted = [p for p in positions if p.mint not in hidden_mints]
    return WalletReading(
        positions=positions,
        total_eur_cents=sum(p.value_eur_cents for p in counted if p.value_eur_cents is not None),
        unpriced_count=sum(1 for p in counted if p.value_eur_cents is None))
