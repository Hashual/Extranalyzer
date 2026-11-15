"""Portefeuilles Solana dans l'application : actualisation manuelle ou planifiée, une seule à la fois.

`read` ouvre une connexion sans verrou, `write` passe par le verrou d'écriture de l'appli. La lecture réseau de
chaque portefeuille se fait hors de ce verrou ; l'écriture est une transaction par portefeuille.
"""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import SolanaSettings
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


class SolanaRuntime:
    def __init__(self, settings: SolanaSettings | None, read: Runner, write: Runner,
                 transport: Transport = urllib_transport, clock: Callable[[], datetime] = _utc_now):
        self.settings, self._read, self._write, self._clock = settings, read, write, clock
        self.client: HeliusClient | None = None
        if settings is not None:
            self.client = HeliusClient(settings.api_key, settings.base_url, transport=transport)
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.client is not None

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
                    rows = conn.execute("SELECT id, address FROM wallets ORDER BY id").fetchall()
                else:
                    rows = conn.execute("SELECT id, address FROM wallets WHERE id = ?", (wallet_id,)).fetchall()
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
