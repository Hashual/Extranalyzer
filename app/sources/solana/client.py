"""Client JSON-RPC de Helius (DAS et RPC Solana). Le transport est injectable : les tests n'appellent jamais le réseau.

L'URL contient la clé (`?api-key=`) : aucun message d'erreur, aucun journal ne contient l'URL ni la clé.
Les adresses ne sont jamais journalisées.
"""
import json
import urllib.parse

from app.sources.enablebanking.client import EnableBankingError, Transport, urllib_transport

BASE_URL = "https://mainnet.helius-rpc.com"
PAGE_LIMIT = 1000
MAX_PAGES = 50
STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
# Disposition d'un compte Stake : après 4 octets de discriminant et 8 de réserve, l'autorité de staking
# (offset 12), puis l'autorité de retrait (offset 44).
STAKER_OFFSET, WITHDRAWER_OFFSET = 12, 44


class HeliusError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RateLimited(HeliusError):
    """Helius limite le nombre de requêtes : réessayer plus tard."""


class HeliusClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, transport: Transport = urllib_transport):
        self._key, self._base, self._transport = api_key, base_url.rstrip("/"), transport

    def _clean(self, text: str) -> str:
        """Message tronqué à 200 caractères, sans la clé ni le nom du paramètre."""
        if self._key:
            text = text.replace(self._key, "***")
        return text.replace("api-key", "***")[:200]

    def _call(self, method: str, params) -> object:
        url = f"{self._base}/?{urllib.parse.urlencode({'api-key': self._key})}"
        body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params}).encode("utf-8")
        try:
            status, raw = self._transport("POST", url, {"Content-Type": "application/json",
                                                        "Accept": "application/json"}, body)
        except EnableBankingError:
            raise HeliusError(0, "NETWORK", "Helius injoignable") from None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        error = data.get("error")
        if status != 200 or error is not None:
            detail = error.get("message") if isinstance(error, dict) else error
            code = str(error.get("code")) if isinstance(error, dict) and error.get("code") is not None else str(status)
            message = self._clean(detail if isinstance(detail, str) and detail else f"erreur HTTP {status}")
            raise (RateLimited if status == 429 else HeliusError)(status, code, message)
        if "result" not in data:
            raise HeliusError(status, "INVALID_JSON", "réponse inattendue de Helius")
        return data["result"]

    def assets_by_owner(self, owner: str) -> tuple[list[dict], dict | None]:
        """Actifs DAS (fongibles et non fongibles) du portefeuille, toutes pages, et solde natif de la première."""
        items: list[dict] = []
        native = None
        for number in range(1, MAX_PAGES + 1):
            result = self._call("getAssetsByOwner", {
                "ownerAddress": owner, "page": number, "limit": PAGE_LIMIT,
                "displayOptions": {"showFungible": True, "showNativeBalance": True}})
            if not isinstance(result, dict):
                raise HeliusError(200, "INVALID_JSON", "réponse inattendue de Helius")
            page = result.get("items") or []
            if not isinstance(page, list):
                raise HeliusError(200, "INVALID_JSON", "réponse inattendue de Helius")
            items += [i for i in page if isinstance(i, dict)]
            if number == 1 and isinstance(result.get("nativeBalance"), dict):
                native = result["nativeBalance"]
            if len(page) < PAGE_LIMIT:
                break
        return items, native

    def asset(self, mint: str) -> dict:
        result = self._call("getAsset", {"id": mint})
        if not isinstance(result, dict):
            raise HeliusError(200, "INVALID_JSON", "réponse inattendue de Helius")
        return result

    def stake_accounts(self, owner: str) -> list[dict]:
        """Comptes Stake dont `owner` est l'autorité de staking ou de retrait, dédoublonnés par `pubkey`."""
        found: dict[str, dict] = {}
        for offset in (STAKER_OFFSET, WITHDRAWER_OFFSET):
            result = self._call("getProgramAccounts", [STAKE_PROGRAM, {
                "encoding": "jsonParsed", "filters": [{"memcmp": {"offset": offset, "bytes": owner}}]}])
            if not isinstance(result, list):
                raise HeliusError(200, "INVALID_JSON", "réponse inattendue de Helius")
            for account in result:
                if isinstance(account, dict) and isinstance(account.get("pubkey"), str):
                    found.setdefault(account["pubkey"], account)
        return list(found.values())

    def epoch(self) -> int:
        result = self._call("getEpochInfo", [])
        if not isinstance(result, dict) or not isinstance(result.get("epoch"), int):
            raise HeliusError(200, "INVALID_JSON", "réponse inattendue de Helius")
        return result["epoch"]
