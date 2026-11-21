"""Lecture des soldes Kraken en positions valorisées en euros, au format des portefeuilles Solana.

- Codes d'actif Kraken normalisés : XXBT → BTC, ZEUR → EUR, SOL.S → SOL placé (staking ou Earn).
- Clé d'actif (`mint`) : l'adresse Solana de la même crypto quand elle est connue (le SOL de Kraken et celui des
  portefeuilles font une seule part dans la répartition), sinon le symbole (`BTC`, `EUR`).
- Prix : paire en euros, sinon paire en dollars convertie par le cours EUR/USD. Aucun prix n'est inventé : une
  position sans paire reste sans valeur et est comptée dans `unpriced_count`.
"""
from decimal import Decimal

from app.importer.amounts import to_cents
from app.sources.kraken.client import KrakenClient
from app.sources.solana.holdings import EURC_MINT, SOL_MINT, Position, WalletReading

EARN_SUFFIXES = {"S", "M", "F", "B", "P", "T"}       # staking, Earn (bloqué, flexible, parachain…)
IGNORED = {"KFEE"}                                   # crédits de frais : pas un avoir
ALIASES = {"XBT": "BTC", "XDG": "DOGE", "ETH2": "ETH"}
RAW_ALIASES = {"ETH2": "XETH"}                       # ETH2 (ancien ETH en staking) se valorise comme l'ETH
EUR, USD = "ZEUR", "ZUSD"
KNOWN_MINTS = {
    "SOL": SOL_MINT,
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "EURC": EURC_MINT,
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
}
FIAT_NAMES = {"EUR": "Euro (espèces)", "USD": "Dollar (espèces)"}


def _decimal(value) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def split_code(code: str) -> tuple[str, bool]:
    """(code Kraken de l'actif de base, placé ?) : 'SOL.S' → ('SOL', True), 'USD.HOLD' → ('ZUSD'…) non placé."""
    base, _, suffix = code.partition(".")
    return RAW_ALIASES.get(base, base), suffix in EARN_SUFFIXES


def symbol_of(base: str, assets: dict[str, dict]) -> str:
    alt = assets.get(base, {}).get("altname")
    if not isinstance(alt, str) or not alt:
        alt = base[1:] if len(base) == 4 and base[0] in "XZ" else base
    return ALIASES.get(alt, alt)


def _pair_index(pairs: dict[str, dict]) -> dict[tuple[str, str], str]:
    index = {}
    for name, info in pairs.items():
        if name.endswith(".d"):              # carnets « dark pool » : même cours, ignorés
            continue
        base, quote = info.get("base"), info.get("quote")
        if isinstance(base, str) and isinstance(quote, str):
            index.setdefault((base, quote), name)
    return index


def _last_price(ticker: dict[str, dict], pair: str | None) -> Decimal | None:
    if pair is None or pair not in ticker:
        return None
    last = ticker[pair].get("c")
    price = _decimal(last[0]) if isinstance(last, list) and last else None
    return price if price is not None and price > 0 else None


def read_account(client: KrakenClient, hidden_mints: frozenset[str] = frozenset()) -> WalletReading:
    balances: dict[tuple[str, bool], Decimal] = {}
    for code, raw in client.balance().items():
        amount = _decimal(raw)
        base, earn = split_code(code)
        if amount is None or amount <= 0 or base in IGNORED:
            continue
        balances[(base, earn)] = balances.get((base, earn), Decimal(0)) + amount
    if not balances:
        return WalletReading(positions=[], total_eur_cents=0, unpriced_count=0)

    assets = client.assets()
    index = _pair_index(client.asset_pairs())
    bases = {b for b, _ in balances} - {EUR}
    eur_pairs = {b: index.get((b, EUR)) for b in bases}
    usd_pairs = {b: index.get((b, USD)) for b in bases if eur_pairs[b] is None}
    eurusd = index.get((EUR, USD)) if any(usd_pairs.values()) or USD in bases else None
    wanted = sorted({p for p in (*eur_pairs.values(), *usd_pairs.values(), eurusd) if p})
    ticker = client.ticker(wanted)
    usd_per_eur = _last_price(ticker, eurusd)

    def price_eur(base: str) -> Decimal | None:
        if base == EUR:
            return Decimal(1)
        if base == USD:
            return None if usd_per_eur is None else 1 / usd_per_eur
        direct = _last_price(ticker, eur_pairs.get(base))
        if direct is not None:
            return direct
        in_usd = _last_price(ticker, usd_pairs.get(base))
        return None if in_usd is None or usd_per_eur is None else in_usd / usd_per_eur

    positions = []
    for (base, earn), amount in sorted(balances.items()):
        symbol = symbol_of(base, assets)
        kind = "stake" if earn else ("sol" if symbol == "SOL" else "token")
        price = price_eur(base)
        positions.append(Position(
            kind, amount, mint=KNOWN_MINTS.get(symbol, symbol), symbol=symbol,
            name=FIAT_NAMES.get(symbol), price_eur=price, price_source=None if price is None else "kraken",
            value_eur_cents=None if price is None else to_cents(amount * price)))
    counted = [p for p in positions if not (p.kind == "token" and p.mint in hidden_mints)]
    return WalletReading(
        positions=positions,
        total_eur_cents=sum(p.value_eur_cents for p in counted if p.value_eur_cents is not None),
        unpriced_count=sum(1 for p in counted if p.value_eur_cents is None))
