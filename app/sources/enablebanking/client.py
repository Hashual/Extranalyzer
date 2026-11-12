"""Client HTTP de l'API Enable Banking. Le transport est injectable : les tests n'appellent jamais le réseau.

Aucun jeton, identifiant de session ni corps de réponse n'est journalisé.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date

from app.sources.enablebanking.jwt import load_private_key, make_jwt

BASE_URL = "https://api.enablebanking.com"
TIMEOUT_S = 30
TOKEN_LIFETIME = 3600
MAX_PAGES = 200

# (méthode, URL, en-têtes, corps) -> (statut HTTP, corps)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class EnableBankingError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class SessionExpired(EnableBankingError):
    """La session (le consentement) n'est plus valable : une nouvelle autorisation est nécessaire."""


class RateLimited(EnableBankingError):
    """La banque limite le nombre de consultations : réessayer plus tard."""


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:   # certificat vérifié par défaut
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EnableBankingError(0, "NETWORK", f"Enable Banking injoignable ({type(exc).__name__})") from None


def _error(status: int, body: bytes) -> EnableBankingError:
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = data["error"] if isinstance(data.get("error"), str) else str(data.get("code") or status)
    message = data["message"] if isinstance(data.get("message"), str) else f"erreur HTTP {status}"
    message = f"{message} ({code})"[:300]
    if status == 429:
        return RateLimited(status, code, message)
    if status == 401 and "SESSION" in code.upper():
        return SessionExpired(status, code, message)
    return EnableBankingError(status, code, message)


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class Client:
    def __init__(self, app_id: str, key_pem: bytes, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time, base_url: str = BASE_URL):
        self._app_id, self._key = app_id, load_private_key(key_pem)
        self._transport, self._clock, self._base = transport, clock, base_url.rstrip("/")
        self._token: tuple[str, int] | None = None     # (jeton, expiration)

    def _bearer(self) -> str:
        now = int(self._clock())
        if self._token is None or self._token[1] - 300 <= now:
            self._token = (make_jwt(self._app_id, self._key, now, TOKEN_LIFETIME), now + TOKEN_LIFETIME)
        return self._token[0]

    def _request(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        status, raw = self._transport(method, url, headers, payload)
        if not 200 <= status < 300:
            raise _error(status, raw)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            raise EnableBankingError(status, "INVALID_JSON", "réponse illisible d'Enable Banking") from None
        if not isinstance(data, dict):
            raise EnableBankingError(status, "INVALID_JSON", "réponse inattendue d'Enable Banking")
        return data

    def aspsps(self, country: str, psu_type: str = "personal") -> list[dict]:
        found = self._request("GET", "/aspsps", {"country": country, "psu_type": psu_type}).get("aspsps")
        return [a for a in found if isinstance(a, dict)] if isinstance(found, list) else []

    def start_auth(self, aspsp_name: str, country: str, valid_until: str, state: str, redirect_url: str,
                   psu_type: str = "personal") -> dict:
        return self._request("POST", "/auth", body={
            "access": {"valid_until": valid_until}, "aspsp": {"name": aspsp_name, "country": country},
            "state": state, "redirect_url": redirect_url, "psu_type": psu_type})

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", body={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{_quoted(session_id)}")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{_quoted(session_id)}")

    def balances(self, uid: str) -> list[dict]:
        found = self._request("GET", f"/accounts/{_quoted(uid)}/balances").get("balances")
        return [b for b in found if isinstance(b, dict)] if isinstance(found, list) else []

    def transactions(self, uid: str, date_from: date | None, strategy: str = "default") -> list[dict]:
        """Toutes les opérations depuis `date_from`, pages suivies par `continuation_key`."""
        path = f"/accounts/{_quoted(uid)}/transactions"
        query = {"date_from": None if date_from is None else date_from.isoformat(), "strategy": strategy}
        found: list[dict] = []
        for _ in range(MAX_PAGES):
            data = self._request("GET", path, query)
            page = data.get("transactions") or []
            if not isinstance(page, list):
                raise EnableBankingError(200, "INVALID_JSON", "liste d'opérations inattendue")
            found += [t for t in page if isinstance(t, dict)]
            key = data.get("continuation_key")
            if not key:
                return found
            query = {**query, "continuation_key": key}
        raise EnableBankingError(200, "TOO_MANY_PAGES", "pagination des opérations interrompue")
