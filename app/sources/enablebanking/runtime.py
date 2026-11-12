"""Enable Banking dans l'application : client, connexion, synchro manuelle ou planifiée, une seule à la fois.

`read` ouvre une connexion sans verrou (récupération réseau), `write` passe par le verrou d'écriture de l'appli.
"""
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from app.config import BankSettings
from app.importer.service import OnNewTransactions
from app.sources.enablebanking.client import Client, EnableBankingError, Transport, urllib_transport
from app.sources.enablebanking.link import (Runner, SyncSummary, complete_connection, consume_state, revoke,
                                            start_connection, sync)

log = logging.getLogger(__name__)

FINISH_WAIT_S = 120      # attente maximale d'une synchro en cours au retour de la banque


class NotConfigured(RuntimeError):
    pass


class SyncBusy(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _committed(fn):
    def run(conn):
        result = fn(conn)
        conn.commit()
        return result
    return run


class BankRuntime:
    def __init__(self, bank: BankSettings | None, read: Runner, write: Runner, on_new: OnNewTransactions,
                 transport: Transport = urllib_transport, clock: Callable[[], datetime] = _utc_now):
        self.bank, self._read, self._write, self._on_new, self._clock = bank, read, write, on_new, clock
        self.client: Client | None = None
        if bank is not None:
            try:
                self.client = Client(bank.app_id, bank.key_pem, transport=transport,
                                     clock=lambda: clock().timestamp())
            except (ValueError, TypeError) as exc:
                log.warning("Enable Banking désactivé : clé privée invalide (%s)", type(exc).__name__)
        self._sync_lock = threading.Lock()

    def write(self, fn):
        """Écriture sous le verrou de l'application."""
        return self._write(fn)

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _client(self) -> Client:
        if self.client is None:
            raise NotConfigured("récupération bancaire non configurée")
        return self.client

    def start(self) -> str:
        client = self._client()
        return start_connection(self._write, client, self.bank, self._clock())

    def finish(self, code: str, state: str) -> SyncSummary:
        """Retour de la banque : `state` vérifié et consommé, session ouverte, comptes rattachés, première synchro
        (l'historique long n'est souvent disponible que dans l'heure qui suit l'autorisation)."""
        client = self._client()
        self._write(_committed(lambda conn: consume_state(conn, state, self._clock())))
        session = client.create_session(code)
        connected = self._write(_committed(lambda conn: complete_connection(conn, client, session, self._clock())))
        self._close_sessions(connected.replaced_sessions)
        # la connexion est établie : attendre une synchro planifiée en cours plutôt que d'échouer
        return self.sync("connexion", wait_s=FINISH_WAIT_S)

    def reject(self, state: str) -> None:
        """Retour en erreur : le `state` est consommé (vérifié) sans ouvrir de session."""
        self._write(_committed(lambda conn: consume_state(conn, state, self._clock())))

    def sync(self, trigger: str, wait_s: float = 0) -> SyncSummary:
        client = self._client()
        acquired = self._sync_lock.acquire(timeout=wait_s) if wait_s > 0 else self._sync_lock.acquire(blocking=False)
        if not acquired:
            raise SyncBusy("une synchronisation est déjà en cours")
        try:
            return sync(self._read, self._write, client, trigger, self._on_new, self._clock)
        finally:
            self._sync_lock.release()

    def disconnect(self) -> None:
        sessions = self._write(_committed(revoke))
        if self.client is not None:
            self._close_sessions(sessions)

    def _close_sessions(self, sessions: list[str]) -> None:
        for session_id in sessions:
            try:
                self._client().delete_session(session_id)
            except EnableBankingError as exc:      # session déjà close côté banque : sans conséquence
                log.info("fermeture d'une ancienne session refusée (%s)", exc.code)

    def scheduled_tick(self) -> None:
        """Un tour de la synchro planifiée ; n'échoue jamais (le thread continue)."""
        if not self.configured:
            return
        try:
            self.sync("planifiée")
        except SyncBusy:
            pass
        except Exception as exc:     # noqa: BLE001 — journalisé sans détail sensible, nouvel essai au tour suivant
            log.error("échec de la synchro bancaire planifiée (%s)", type(exc).__name__)
