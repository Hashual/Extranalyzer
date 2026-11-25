"""Portefeuilles Solana dans l'application : actualisation manuelle ou planifiée, une seule à la fois.

`read` ouvre une connexion sans verrou, `write` passe par le verrou d'écriture de l'appli. La lecture réseau de
chaque portefeuille se fait hors de ce verrou ; l'écriture est une transaction par portefeuille.
"""
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import DEFAULT_HELIUS_URL, SolanaSettings
from app.sources.enablebanking.client import Transport, urllib_transport
from app.sources.enablebanking.link import Runner
from app.sources.solana import wallets
from app.sources.solana.client import HeliusClient, HeliusError, RateLimited
from app.sources.solana.holdings import NoEurRate, read_wallet

log = logging.getLogger(__name__)

NO_EUR_RATE_MESSAGE = "taux euro indisponible (prix de l'EURC absent)"
RATE_LIMITED_MESSAGE = "limite de requêtes Helius atteinte, réessayez plus tard"


class NotConfigured(RuntimeError):
    pass


class RefreshBusy(RuntimeError):
    pass


@dataclass
class RefreshSummary:
    refreshed: int
    errors: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _committed(fn):
    def run(conn):
        result = fn(conn)
        conn.commit()
        return result
    return run


class KeyInvalid(ValueError):
    """Clé mal formée, ou refusée par Helius (message destiné à l'utilisateur)."""


class KeyManagedByServer(RuntimeError):
    """La clé vient de la configuration du serveur (secret Docker) : elle ne se change pas depuis l'interface."""


KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")


def _read_stored_key(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        log.warning("clé Helius enregistrée illisible (%s)", type(exc).__name__)
        return None
    return key if KEY_PATTERN.fullmatch(key) else None


class SolanaRuntime:
    """Clé Helius : celle du serveur (`settings`, secret Docker) si elle existe, sinon celle saisie dans
    l'interface et enregistrée dans `key_file` (volume de données, droits 600)."""

    def __init__(self, settings: SolanaSettings | None, read: Runner, write: Runner,
                 transport: Transport = urllib_transport, clock: Callable[[], datetime] = _utc_now,
                 key_file: Path | None = None, base_url: str | None = DEFAULT_HELIUS_URL):
        self.settings, self._read, self._write, self._clock = settings, read, write, clock
        self._transport, self._key_file = transport, key_file
        self._base_url = settings.base_url if settings is not None else base_url
        self.client: HeliusClient | None = None
        self.key_source: str | None = None          # 'server' | 'app' | None
        if settings is not None:
            self.client, self.key_source = HeliusClient(settings.api_key, self._base_url, transport=transport), "server"
        elif self._base_url is not None:
            stored = _read_stored_key(key_file)
            if stored is not None:
                self.client, self.key_source = HeliusClient(stored, self._base_url, transport=transport), "app"
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def can_set_key(self) -> bool:
        return self.key_source != "server" and self._key_file is not None and self._base_url is not None

    def set_key(self, api_key: str) -> None:
        """Vérifie la clé auprès de Helius (un appel en lecture), puis l'enregistre et l'utilise.

        Lève KeyManagedByServer, KeyInvalid (forme ou refus de Helius) ou HeliusError (Helius injoignable, quota).
        """
        if self.key_source == "server":
            raise KeyManagedByServer("la clé Helius est fournie par la configuration du serveur")
        if not self.can_set_key:
            raise NotConfigured("adresse du service Helius invalide (EXTRANALYZER_HELIUS_URL)")
        key = (api_key or "").strip()
        if not KEY_PATTERN.fullmatch(key):
            raise KeyInvalid("clé Helius invalide : collez la clé seule (lettres, chiffres et tirets)")
        candidate = HeliusClient(key, self._base_url, transport=self._transport)
        try:
            candidate.epoch()
        except RateLimited:
            raise
        except HeliusError as exc:
            if exc.status in (401, 403):
                raise KeyInvalid("clé refusée par Helius : vérifiez-la dans votre tableau de bord Helius") from None
            raise
        tmp = self._key_file.with_name(self._key_file.name + ".tmp")
        try:
            tmp.unlink(missing_ok=True)      # jamais un .tmp préexistant aux droits plus larges
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._key_file)
        except OSError:
            tmp.unlink(missing_ok=True)      # aucune copie de la clé ne reste derrière un échec d'écriture
            raise
        self.client, self.key_source = candidate, "app"
        log.info("clé Helius enregistrée depuis l'interface")

    def clear_key(self) -> None:
        """Oublie la clé saisie dans l'interface (le fichier est supprimé)."""
        if self.key_source == "server":
            raise KeyManagedByServer("la clé Helius est fournie par la configuration du serveur")
        if self._key_file is not None:
            self._key_file.unlink(missing_ok=True)
        self.client, self.key_source = None, None

    def write(self, fn):
        """Écriture sous le verrou de l'application."""
        return self._write(fn)

    def refresh(self, trigger: str, wallet_id: int | None = None) -> RefreshSummary:
        """Relève un portefeuille (`wallet_id`) ou tous ; l'échec de l'un n'arrête pas les autres."""
        client = self.client
        if client is None:
            raise NotConfigured("portefeuilles Solana non configurés")
        if not self._lock.acquire(blocking=False):
            raise RefreshBusy("une actualisation est déjà en cours")
        try:
            def load(conn):
                if wallet_id is None:
                    rows = conn.execute("SELECT id, address FROM wallets WHERE provider = 'solana' ORDER BY id").fetchall()
                else:
                    rows = conn.execute("SELECT id, address FROM wallets WHERE id = ? AND provider = 'solana'",
                                        (wallet_id,)).fetchall()
                return rows, wallets.hidden_mints(conn)
            targets, hidden = self._read(load)
            summary = RefreshSummary(refreshed=0, errors=0)
            for target_id, address in targets:
                reading, error = None, None
                try:
                    reading = read_wallet(client, address, hidden)
                except RateLimited:
                    error = RATE_LIMITED_MESSAGE
                except HeliusError as exc:
                    error = exc.message
                except NoEurRate:
                    error = NO_EUR_RATE_MESSAGE
                except Exception as exc:     # noqa: BLE001 — une réponse mal formée n'arrête pas les autres
                    error = f"réponse inattendue de Helius ({type(exc).__name__})"
                    log.error("lecture d'un portefeuille Solana impossible (%s)", type(exc).__name__)
                taken_at = self._clock().isoformat()
                def record(conn, i=target_id, r=reading, m=error):
                    # portefeuille supprimé pendant la lecture réseau : rien à enregistrer
                    if conn.execute("SELECT 1 FROM wallets WHERE id = ?", (i,)).fetchone() is None:
                        return False
                    if r is not None:
                        wallets.record_reading(conn, i, taken_at, r)
                    else:
                        wallets.record_error(conn, i, taken_at, m)
                    return True
                if not self._write(_committed(record)):
                    continue
                if reading is not None:
                    summary.refreshed += 1
                else:
                    summary.errors += 1
            log.info("actualisation des portefeuilles Solana (%s) : %d relevé(s), %d erreur(s)", trigger,
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
            log.error("échec de l'actualisation planifiée des portefeuilles Solana (%s)", type(exc).__name__)
