"""Jeton JWT RS256 d'Enable Banking, signé avec `cryptography` (aucune dépendance supplémentaire).

En-tête `typ=JWT`, `alg=RS256`, `kid=<identifiant de l'application>` ; charges `iss`, `aud`, `iat`, `exp`
(durée maximale acceptée par l'API : 24 h).
"""
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISSUER = "enablebanking.com"
AUDIENCE = "api.enablebanking.com"
MAX_LIFETIME = 86400


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_private_key(key_pem: bytes) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(key_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("la clé Enable Banking doit être une clé privée RSA")
    return key


def make_jwt(app_id: str, key: rsa.RSAPrivateKey, now: int, lifetime: int = 3600) -> str:
    if not 0 < lifetime <= MAX_LIFETIME:
        raise ValueError(f"durée du jeton hors de ]0, {MAX_LIFETIME}] : {lifetime}")
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    payload = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + lifetime}
    signing_input = ".".join(_b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, payload))
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"
