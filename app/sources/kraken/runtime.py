"""Compte Kraken dans l'application : clé saisie dans l'interface, actualisation manuelle ou planifiée.

Le compte est une ligne de `wallets` (`provider = 'kraken'`) : relevés et positions sont ceux des portefeuilles
Solana, ce qui l'intègre tel quel à l'onglet Épargne. La lecture réseau se fait hors du verrou d'écriture.
"""
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from app.classify.savings_target import savings_category_named
from app.sources.enablebanking.client import Transport, urllib_transport
from app.sources.enablebanking.link import Runner
from app.sources.kraken.client import BASE_URL, AuthError, KrakenClient, KrakenError, RateLimited, decode_secret
from app.sources.kraken.holdings import read_account
from app.sources.solana import wallets
from app.sources.solana.runtime import RefreshBusy, RefreshSummary

log = logging.getLogger(__name__)

PROVIDER = "kraken"
DEFAULT_LABEL = "Kraken"
RATE_LIMITED_MESSAGE = "limite de requêtes Kraken atteinte, réessayez plus tard"
KEY_PATTERN = re.compile(r"[A-Za-z0-9+/=]{16,128}")


class NotConfigured(RuntimeError):
    pass


class KeyInvalid(ValueError):
    """Clé mal formée, ou refusée par Kraken (message destiné à l'utilisateur)."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _committed(fn):
    def run(conn):
        result = fn(conn)
        conn.commit()
        return result
    return run


def account_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM wallets WHERE provider = ? ORDER BY id LIMIT 1", (PROVIDER,)).fetchone()
    return None if row is None else row[0]


def ensure_account(conn: sqlite3.Connection, now: str) -> int:
    """Le compte Kraken (créé au premier enregistrement de clé, avec sa catégorie d'épargne « Kraken »)."""
    found = account_id(conn)
    if found is not None:
        return found
    category_id = savings_category_named(conn, DEFAULT_LABEL)
    return conn.execute("INSERT INTO wallets(address, label, savings_category_id, created_at, provider) "
                        "VALUES (?, ?, ?, ?, ?)", (PROVIDER, DEFAULT_LABEL, category_id, now, PROVIDER)).lastrowid


def _read_stored_key(path: Path | None) -> tuple[str, str] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key, secret = data["api_key"], data["api_secret"]
        if KEY_PATTERN.fullmatch(key):
            decode_secret(secret)
            return key, secret
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        log.warning("clé Kraken enregistrée illisible (%s)", type(exc).__name__)
    return None


class KrakenRuntime:
    def __init__(self, read: Runner, write: Runner, transport: Transport = urllib_transport,
                 clock: Callable[[], datetime] = _utc_now, key_file: Path | None = None, base_url: str = BASE_URL):
        self._read, self._write, self._clock = read, write, clock
        self._transport, self._key_file, self._base_url = transport, key_file, base_url
        self.client: KrakenClient | None = None
        stored = _read_stored_key(key_file)
        if stored is not None:
            self.client = KrakenClient(*stored, base_url, transport=transport)
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def can_set_key(self) -> bool:
        return self._key_file is not None

    def set_key(self, api_key: str, api_secret: str) -> None:
        """Vérifie la clé auprès de Kraken (lecture des soldes), l'enregistre, puis crée le compte s'il manque.

        Lève KeyInvalid (forme, refus ou permission manquante), RateLimited ou KrakenError (Kraken injoignable).
        """
        if not self.can_set_key:
            raise NotConfigured("emplacement de la clé Kraken indisponible")
        key, secret = (api_key or "").strip(), "".join((api_secret or "").split())
        if not KEY_PATTERN.fullmatch(key):
            raise KeyInvalid("clé API Kraken invalide : collez la « clé API » seule")
        try:
            candidate = KrakenClient(key, secret, self._base_url, transport=self._transport)
        except ValueError:
            raise KeyInvalid("clé privée Kraken invalide : collez la « clé privée » complète") from None
        try:
            candidate.balance()
        except AuthError as exc:
            if exc.permission:
                raise KeyInvalid("clé sans la permission « Query Funds » : cochez-la dans Kraken") from None
            raise KeyInvalid("clé refusée par Kraken : vérifiez la clé API et la clé privée") from None
        tmp = self._key_file.with_name(self._key_file.name + ".tmp")
        try:
            tmp.unlink(missing_ok=True)      # jamais un .tmp préexistant aux droits plus larges
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"api_key": key, "api_secret": secret}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._key_file)
        except OSError:
            tmp.unlink(missing_ok=True)      # aucune copie de la clé ne reste derrière un échec d'écriture
            raise
        self.client = candidate
        now = self._clock().isoformat()
        self._write(_committed(lambda conn: ensure_account(conn, now)))
        log.info("clé Kraken enregistrée depuis l'interface")

    def clear_key(self) -> None:
        """Oublie la clé (le fichier est supprimé) ; le compte et son historique restent."""
        if self._key_file is not None:
            self._key_file.unlink(missing_ok=True)
        self.client = None

    def refresh(self, trigger: str) -> RefreshSummary:
        client = self.client
        if client is None:
            raise NotConfigured("compte Kraken non relié")
        if not self._lock.acquire(blocking=False):
            raise RefreshBusy("une actualisation est déjà en cours")
        try:
            target, hidden = self._read(lambda conn: (account_id(conn), wallets.hidden_mints(conn)))
            if target is None:
                now = self._clock().isoformat()
                target = self._write(_committed(lambda conn: ensure_account(conn, now)))
            reading, error = None, None
            try:
                reading = read_account(client, hidden)
            except RateLimited:
                error = RATE_LIMITED_MESSAGE
            except AuthError as exc:
                error = ("clé sans la permission « Query Funds »" if exc.permission
                         else "clé refusée par Kraken : enregistrez-la de nouveau")
            except KrakenError as exc:
                error = exc.message
            except Exception as exc:     # noqa: BLE001 — réponse mal formée : erreur enregistrée, pas de plantage
                error = f"réponse inattendue de Kraken ({type(exc).__name__})"
                log.error("lecture du compte Kraken impossible (%s)", type(exc).__name__)
            taken_at = self._clock().isoformat()

            def record(conn):
                if conn.execute("SELECT 1 FROM wallets WHERE id = ?", (target,)).fetchone() is None:
                    return False         # compte retiré pendant la lecture réseau
                if reading is not None:
                    wallets.record_reading(conn, target, taken_at, reading)
                else:
                    wallets.record_error(conn, target, taken_at, error)
                return True
            recorded = self._write(_committed(record))
            summary = RefreshSummary(refreshed=int(recorded and reading is not None),
                                     errors=int(recorded and reading is None))
            log.info("actualisation du compte Kraken (%s) : %d relevé(s), %d erreur(s)", trigger,
                     summary.refreshed, summary.errors)
            return summary
        finally:
            self._lock.release()

    def scheduled_tick(self) -> None:
        """Un tour de la synchro planifiée ; n'échoue jamais (le thread continue)."""
        if not self.configured:
            return
        try:
            self.refresh("planifiée")
        except RefreshBusy:
            pass
        except Exception as exc:     # noqa: BLE001 — journalisé sans détail sensible, nouvel essai au tour suivant
            log.error("échec de l'actualisation planifiée du compte Kraken (%s)", type(exc).__name__)
