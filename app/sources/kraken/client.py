"""Client REST de Kraken : soldes (privé, signé) et cours (public). Le transport est injectable : les tests n'appellent
jamais le réseau.

Signature : API-Sign = base64(HMAC-SHA512(base64decode(clé privée), chemin + SHA256(nonce + corps))). Aucun message
d'erreur, aucun journal ne contient la clé, la clé privée ni la signature.
"""
import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
import urllib.parse

from app.sources.enablebanking.client import EnableBankingError, Transport, urllib_transport

BASE_URL = "https://api.kraken.com"
RATE_LIMIT_ERRORS = ("EAPI:Rate limit exceeded", "EGeneral:Too many requests", "EService:Throttled")
AUTH_ERRORS = ("EAPI:Invalid key", "EAPI:Invalid signature", "EAPI:Bad request")
PERMISSION_ERROR = "EGeneral:Permission denied"


class KrakenError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RateLimited(KrakenError):
    """Kraken limite le nombre de requêtes : réessayer plus tard."""


class AuthError(KrakenError):
    """Clé refusée par Kraken (inconnue, clé privée fausse) ou sans la permission demandée (`permission`)."""

    @property
    def permission(self) -> bool:
        return self.code == PERMISSION_ERROR


def decode_secret(secret: str) -> bytes:
    """Clé privée Kraken (base64) en octets ; ValueError si elle n'est pas du base64 valide."""
    try:
        raw = base64.b64decode(secret, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("clé privée invalide") from None
    if len(raw) < 32:
        raise ValueError("clé privée invalide")
    return raw


def sign(secret: bytes, path: str, nonce: str, post: str) -> str:
    digest = hashlib.sha256((nonce + post).encode("utf-8")).digest()
    return base64.b64encode(hmac.new(secret, path.encode("utf-8") + digest, hashlib.sha512).digest()).decode("ascii")


class _Nonce:
    """Nonce strictement croissant (millisecondes), partagé par tous les clients du processus."""

    def __init__(self):
        self._last, self._lock = 0, threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._last = max(self._last + 1, int(time.time() * 1000))
            return self._last


NONCE = _Nonce()


class KrakenClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL, transport: Transport = urllib_transport):
        self._key, self._secret = api_key, decode_secret(api_secret)
        self._base, self._transport = base_url.rstrip("/"), transport

    def _clean(self, text: str) -> str:
        if self._key:
            text = text.replace(self._key, "***")
        return text[:200]

    def _send(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> object:
        try:
            status, raw = self._transport(method, self._base + path, headers, body)
        except EnableBankingError:
            raise KrakenError(0, "NETWORK", "Kraken injoignable") from None
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        errors = [e for e in (data.get("error") or []) if isinstance(e, str)] if isinstance(data.get("error"), list) else []
        if status == 429 or any(e.startswith(RATE_LIMIT_ERRORS) for e in errors):
            raise RateLimited(status, errors[0] if errors else "429", "limite de requêtes Kraken atteinte")
        if errors:
            code = errors[0]
            if code.startswith(AUTH_ERRORS) or code == PERMISSION_ERROR:
                raise AuthError(status, code, self._clean(code))
            raise KrakenError(status, code, self._clean(f"erreur Kraken ({code})"))
        if status != 200:
            raise KrakenError(status, str(status), f"erreur HTTP {status}")
        if not isinstance(data.get("result"), dict):
            raise KrakenError(status, "INVALID_JSON", "réponse inattendue de Kraken")
        return data["result"]

    def _private(self, endpoint: str, params: dict[str, str] | None = None) -> dict:
        path = f"/0/private/{endpoint}"
        nonce = str(NONCE.next())
        post = urllib.parse.urlencode({"nonce": nonce, **(params or {})})
        headers = {"API-Key": self._key, "API-Sign": sign(self._secret, path, nonce, post), "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        return self._send("POST", path, headers, post.encode("utf-8"))

    def _public(self, endpoint: str, params: dict[str, str] | None = None) -> dict:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._send("GET", f"/0/public/{endpoint}{query}", {"Accept": "application/json"}, None)

    def balance(self) -> dict[str, str]:
        """Soldes par code d'actif Kraken (ex. XXBT, ZEUR, SOL.S), montants en chaînes décimales."""
        return {k: v for k, v in self._private("Balance").items() if isinstance(k, str) and isinstance(v, str)}

    def assets(self) -> dict[str, dict]:
        return {k: v for k, v in self._public("Assets").items() if isinstance(v, dict)}

    def asset_pairs(self) -> dict[str, dict]:
        return {k: v for k, v in self._public("AssetPairs").items() if isinstance(v, dict)}

    def ticker(self, pairs: list[str]) -> dict[str, dict]:
        if not pairs:
            return {}
        return {k: v for k, v in self._public("Ticker", {"pair": ",".join(pairs)}).items() if isinstance(v, dict)}
