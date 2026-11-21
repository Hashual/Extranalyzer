"""Faux Kraken pour les tests : un transport REST qui rejoue des réponses fictives, sans réseau.

Chaque endpoint (`Balance`, `Assets`, `AssetPairs`, `Ticker`) a une file de réponses ; la dernière est rejouée
indéfiniment. Les appels privés sont refusés si la signature ne correspond pas à la clé privée attendue.
"""
import base64
import json
import os
import urllib.parse

from app.sources.kraken.client import sign

API_KEY = "CleApiFictiveKraken0123456789abcdefABCDEF+/"
API_SECRET = base64.b64encode(b"cle-privee-fictive-de-kraken-64-octets-pour-les-tests-unitaires!").decode()

ASSETS = {"XXBT": {"altname": "XBT"}, "XETH": {"altname": "ETH"}, "ZEUR": {"altname": "EUR"},
          "ZUSD": {"altname": "USD"}, "SOL": {"altname": "SOL"}, "DOT": {"altname": "DOT"},
          "USDC": {"altname": "USDC"}, "OBSC": {"altname": "OBSC"}}
PAIRS = {"XXBTZEUR": {"base": "XXBT", "quote": "ZEUR"}, "XXBTZEUR.d": {"base": "XXBT", "quote": "ZEUR"},
         "XETHZEUR": {"base": "XETH", "quote": "ZEUR"}, "SOLEUR": {"base": "SOL", "quote": "ZEUR"},
         "DOTUSD": {"base": "DOT", "quote": "ZUSD"}, "USDCEUR": {"base": "USDC", "quote": "ZEUR"},
         "ZEURZUSD": {"base": "ZEUR", "quote": "ZUSD"}}
TICKER = {"XXBTZEUR": {"c": ["60000.0", "0.1"]}, "XETHZEUR": {"c": ["3000.0", "1"]},
          "SOLEUR": {"c": ["150.0", "1"]}, "DOTUSD": {"c": ["6.0", "1"]}, "USDCEUR": {"c": ["0.92", "1"]},
          "ZEURZUSD": {"c": ["1.2", "1"]}}


class FakeKraken:
    def __init__(self, secret: str = API_SECRET):
        self._secret = base64.b64decode(secret)
        self.routes: dict[str, list[tuple[int, object]]] = {}
        self.requests: list[dict] = []
        self.on("Assets", ASSETS).on("AssetPairs", PAIRS).on("Ticker", TICKER)

    def on(self, endpoint: str, *responses):
        """Réponses successives : un résultat (statut 200, enveloppé) ou un tuple (statut, corps brut)."""
        self.routes[endpoint] = [r if isinstance(r, tuple) else (200, {"error": [], "result": r}) for r in responses]
        return self

    def error(self, endpoint: str, code: str, status: int = 200):
        return self.on(endpoint, (status, {"error": [code]}))

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlsplit(url)
        endpoint = os.path.basename(parsed.path)
        form = dict(urllib.parse.parse_qsl(body.decode())) if body else {}
        self.requests.append({"http_method": method, "path": parsed.path, "endpoint": endpoint, "headers": headers,
                              "query": dict(urllib.parse.parse_qsl(parsed.query)), "form": form})
        if parsed.path.startswith("/0/private/"):
            expected = sign(self._secret, parsed.path, form.get("nonce", ""), body.decode())
            if headers.get("API-Sign") != expected:
                return 200, json.dumps({"error": ["EAPI:Invalid signature"]}).encode()
        queue = self.routes.get(endpoint)
        if not queue:
            return 404, json.dumps({"error": ["EGeneral:Unknown method"]}).encode()
        status, content = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, json.dumps(content).encode()

    def calls(self, endpoint: str) -> list[dict]:
        return [r for r in self.requests if r["endpoint"] == endpoint]
