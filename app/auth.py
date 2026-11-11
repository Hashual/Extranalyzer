"""Mot de passe (scrypt), cookie de session signé et limitation des tentatives de connexion."""
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "xa_session"
SESSION_MAX_AGE = 30 * 24 * 3600
_SESSION_SALT = "xa-session"

_SALT_LEN = 16
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> bytes:
    """Renvoie `sel || scrypt(mot de passe)` ; le sel est tiré au hasard à chaque appel."""
    salt = secrets.token_bytes(_SALT_LEN)
    return salt + hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def verify_password(password: str, stored: bytes) -> bool:
    salt, expected = stored[:_SALT_LEN], stored[_SALT_LEN:]
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(actual, expected)


SECRET_LEN = 32


def _write_secret_atomically(path: Path) -> None:
    """Écrit une clé neuve dans un fichier temporaire 0600, la synchronise puis la met en place."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(SECRET_LEN))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_or_create_secret(path: Path) -> bytes:
    """Clé de signature des sessions, créée en 0600 si absente (au mieux sous Windows).

    Échoue fermé : une clé existante de moins de 32 octets (vide, tronquée) arrête le démarrage
    plutôt que de signer des sessions falsifiables.
    """
    if not path.exists():
        _write_secret_atomically(path)
    secret = path.read_bytes()
    if len(secret) < SECRET_LEN:
        raise RuntimeError(f"{path.name} invalide ({len(secret)} octets, {SECRET_LEN} minimum) : "
                           f"supprimer le fichier pour en générer un nouveau")
    return secret


class Sessions:
    def __init__(self, secret: bytes):
        self._serializer = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)

    def issue(self) -> str:
        return self._serializer.dumps({"v": 1})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self._serializer.loads(token, max_age=SESSION_MAX_AGE)
        except BadSignature:       # inclut SignatureExpired
            return False
        return True


class RateLimiter:
    """Au plus `limit` échecs par fenêtre glissante de `window` secondes, par adresse IP.

    Chaque tentative est comptée dès `acquire` (vérification et comptage atomiques : des requêtes
    parallèles ne peuvent pas dépasser la limite) puis décomptée par `succeeded` si elle réussit.
    """

    def __init__(self, limit: int = 5, window: float = 60, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for ip in list(self._attempts):
            hits = self._attempts[ip]
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if not hits:
                del self._attempts[ip]

    def acquire(self, ip: str) -> bool:
        """Faux si l'adresse a atteint la limite ; sinon compte la tentative et renvoie vrai."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            hits = self._attempts.setdefault(ip, deque())
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def succeeded(self, ip: str) -> None:
        with self._lock:
            hits = self._attempts.get(ip)
            if hits:
                hits.pop()
