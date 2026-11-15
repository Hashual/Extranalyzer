"""Faux serveur Enable Banking pour les tests : un transport qui rejoue des réponses JSON fictives.

Aucun appel réseau. Chaque route est une file de réponses (statut, corps) ; la dernière est rejouée indéfiniment.
"""
import json
import urllib.parse
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@lru_cache(maxsize=1)
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def rsa_key_pem() -> bytes:
    return rsa_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())


class FakeBank:
    def __init__(self):
        self.routes: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.requests: list[dict] = []

    def on(self, method: str, path: str, *responses):
        """Réponses successives : un dict/list (statut 200) ou un tuple (statut, corps)."""
        self.routes[(method, path)] = [r if isinstance(r, tuple) else (200, r) for r in responses]
        return self

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.requests.append({"method": method, "path": parsed.path, "query": query, "headers": headers,
                              "body": None if body is None else json.loads(body)})
        queue = self.routes.get((method, parsed.path))
        if not queue:
            return 404, json.dumps({"message": "route inconnue du faux serveur", "error": "NOT_FOUND"}).encode()
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(payload):
            status, payload = payload(query)
        return status, json.dumps(payload).encode()

    def calls(self, method: str, path: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]


def booked(day: str, amount: str, label: str, indicator: str = "DBIT", **extra) -> dict:
    return {"transaction_amount": {"amount": amount, "currency": "EUR"}, "credit_debit_indicator": indicator,
            "status": "BOOK", "booking_date": day, "value_date": day, "remittance_information": [label], **extra}
