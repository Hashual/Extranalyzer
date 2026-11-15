"""Faux Helius pour les tests : un transport JSON-RPC qui rejoue des réponses fictives, sans réseau.

Chaque méthode JSON-RPC a une file de réponses ; la dernière est rejouée indéfiniment.
"""
import json
import urllib.parse


class FakeHelius:
    def __init__(self):
        self.routes: dict[str, list[tuple[int, object]]] = {}
        self.requests: list[dict] = []

    def on(self, method: str, *responses):
        """Réponses successives : un résultat (statut 200, enveloppé en JSON-RPC) ou un tuple (statut, corps brut)."""
        self.routes[method] = [r if isinstance(r, tuple) else (200, {"jsonrpc": "2.0", "id": "1", "result": r})
                               for r in responses]
        return self

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlsplit(url)
        payload = json.loads(body) if body else {}
        self.requests.append({"http_method": method, "path": parsed.path, "query": dict(urllib.parse.parse_qsl(parsed.query)),
                              "headers": headers, "method": payload.get("method"), "params": payload.get("params"),
                              "body": payload})
        queue = self.routes.get(payload.get("method"))
        if not queue:
            return 404, json.dumps({"error": {"code": -32601, "message": "méthode inconnue du faux serveur"}}).encode()
        status, content = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, json.dumps(content).encode()

    def calls(self, method: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method]
