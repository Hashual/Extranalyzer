"""Relève IMAP (Gmail, lecture seule) des tickets de caisse.

Gmail n'est jamais modifié : `select(readonly=True)` et `BODY.PEEK[]` ; aucun drapeau, aucune copie,
aucune suppression. Aucun journal ne contient d'adresse, de mot de passe ni de contenu de ticket.
"""
import email
import email.policy
import imaplib
import logging
import sqlite3
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from app.config import MailSettings
from app.receipts.base import ReceiptParser
from app.receipts.service import ingest_file
from app.store.db import transactional

log = logging.getLogger(__name__)

T = TypeVar("T")
Write = Callable[[Callable[[sqlite3.Connection], T]], T]

HOST, PORT, TIMEOUT_S = "imap.gmail.com", 993, 60
MAX_ERROR_LEN = 160


@dataclass
class FetchedMessage:
    label: str
    store: str
    uidvalidity: int
    uid: int
    message_id: str | None
    attachments: list[tuple[str, bytes]]        # (nom, contenu) des PDF seulement


class MailboxError(Exception):
    """Libellé inaccessible (absent, refusé) : n'arrête pas les autres libellés."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pdf_attachments(msg: email.message.EmailMessage) -> list[tuple[str, bytes]]:
    found = []
    for part in msg.walk():
        name = part.get_filename() or ""
        if part.is_multipart() or not (name.lower().endswith(".pdf") or part.get_content_type() == "application/pdf"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((name or "ticket.pdf", payload))
    return found


def _select(imap, label: str) -> int:
    """Ouvre le libellé en lecture seule et renvoie son UIDVALIDITY."""
    typ, _ = imap.select(f'"{label}"', readonly=True)
    if typ != "OK":
        raise MailboxError("libellé introuvable")
    _, data = imap.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        raise MailboxError("UIDVALIDITY illisible") from None


def _new_uids(imap, last: int) -> list[int]:
    typ, data = imap.uid("SEARCH", f"UID {last + 1}:*")
    if typ != "OK":
        raise MailboxError("recherche refusée")
    # Pour une plage vide, le serveur renvoie quand même le dernier message : on ignore les UID <= last.
    return sorted(u for u in map(int, (data[0] or b"").split()) if u > last)


def _fetch_raw(imap, uid: int) -> bytes | None:
    typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        return None
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    return None


def fetch_new(cfg: MailSettings, state: dict[str, tuple[int | None, int]], connect=imaplib.IMAP4_SSL,
              errors: dict[str, str] | None = None) -> Iterator[FetchedMessage]:
    """Messages nouveaux de chaque libellé, un par un (mémoire bornée).

    `state` : {libellé: (uidvalidity connue, dernier UID traité)}. Un libellé inaccessible est noté dans
    `errors` (si fourni) et le suivant est traité ; une erreur de connexion se propage.
    """
    imap = connect(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S)
    try:
        imap.login(cfg.user, cfg.app_password)
        for label, store in cfg.labels:
            known_validity, last = state.get(label, (None, 0))
            try:
                validity = _select(imap, label)
                if known_validity is not None and known_validity != validity:
                    last = 0            # les UID ont été renumérotés : on repart de zéro
                for uid in _new_uids(imap, last):
                    raw = _fetch_raw(imap, uid)
                    if raw is None:
                        raise MailboxError("lecture du message refusée")
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    message_id = (msg["Message-ID"] or "").strip() or None
                    yield FetchedMessage(label, store, validity, uid, message_id, _pdf_attachments(msg))
            except MailboxError as exc:
                if errors is None:
                    raise
                errors[label] = str(exc)
    finally:
        try:
            imap.logout()
        except Exception:       # noqa: BLE001 - la fermeture ne doit pas masquer l'erreur d'origine
            pass


def _short_error(exc: BaseException, cfg: MailSettings) -> str:
    text = " ".join(str(exc).split())
    for secret in (cfg.user, cfg.app_password):
        if secret:
            text = text.replace(secret, "***")
    return f"{type(exc).__name__}: {text}"[:MAX_ERROR_LEN]


def _read_state(conn: sqlite3.Connection) -> dict[str, tuple[int | None, int]]:
    return {label: (validity, last)
            for label, validity, last in conn.execute("SELECT label, uidvalidity, last_uid FROM mail_sync")}


def _prepare(cfg: MailSettings, now: str) -> Callable[[sqlite3.Connection], dict[str, tuple[int | None, int]]]:
    def run(conn: sqlite3.Connection):
        for label, store in cfg.labels:
            conn.execute("INSERT INTO mail_sync(label, store, last_attempt_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(label) DO UPDATE SET store=excluded.store, "
                         "last_attempt_at=excluded.last_attempt_at", (label, store, now))
        return _read_state(conn)
    return transactional(run)


def _ingest_message(msg: FetchedMessage, receipts_dir: Path,
                    parsers: list[ReceiptParser]) -> Callable[[sqlite3.Connection], int]:
    """Une transaction par message : ses pièces jointes et l'avancement de `last_uid` réussissent ensemble."""
    def run(conn: sqlite3.Connection) -> int:
        files = 0
        known = msg.message_id and conn.execute("SELECT 1 FROM receipts WHERE message_id=?",
                                                (msg.message_id,)).fetchone()
        if not known:
            for name, data in msg.attachments:
                result = ingest_file(conn, data, name, receipts_dir, parsers, origin="imap",
                                     store=msg.store, message_id=msg.message_id)
                files += result.outcome in ("stored", "slip")
        conn.execute("UPDATE mail_sync SET last_uid=?, uidvalidity=? WHERE label=?",
                     (msg.uid, msg.uidvalidity, msg.label))
        return -1 if known else files        # -1 : message déjà connu, ignoré
    return transactional(run)


def _finish(cfg: MailSettings, now: str, label_errors: dict[str, str],
            global_error: str | None) -> Callable[[sqlite3.Connection], None]:
    def run(conn: sqlite3.Connection) -> None:
        for label, _ in cfg.labels:
            error = global_error or label_errors.get(label)
            if error:
                conn.execute("UPDATE mail_sync SET last_error=?, last_attempt_at=? WHERE label=?",
                             (error, now, label))
            else:
                conn.execute("UPDATE mail_sync SET last_error=NULL, last_success_at=?, last_attempt_at=? "
                             "WHERE label=?", (now, now, label))
    return transactional(run)


def sync_once(cfg: MailSettings, write: Write, receipts_dir: Path, parsers: list[ReceiptParser],
              connect=imaplib.IMAP4_SSL) -> dict:
    """Un passage de relève. Ne lève jamais d'erreur de réseau ou d'IMAP : elle est inscrite dans mail_sync."""
    now = _now()
    state = write(_prepare(cfg, now))
    messages = files = 0
    label_errors: dict[str, str] = {}
    global_error = None
    try:
        for msg in fetch_new(cfg, state, connect, label_errors):
            count = write(_ingest_message(msg, receipts_dir, parsers))
            if count >= 0:
                messages += 1
                files += count
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, sqlite3.Error) as exc:
        global_error = _short_error(exc, cfg)
    except Exception as exc:        # noqa: BLE001 - la relève ne doit jamais lever ; l'échec est visible dans mail_sync
        log.exception("relève des tickets interrompue")
        global_error = _short_error(exc, cfg)
    write(_finish(cfg, _now(), label_errors, global_error))
    error = global_error or next(iter(label_errors.values()), None)
    if error:
        log.warning("relève des tickets : %s", error)
    return {"messages": messages, "files": files, "error": error}


class MailSyncThread(threading.Thread):
    """Relève au démarrage puis toutes les `interval_min` minutes ; `trigger()` force un passage."""

    def __init__(self, cfg: MailSettings, write: Write, receipts_dir: Path, parsers: list[ReceiptParser],
                 connect=imaplib.IMAP4_SSL, lock=None):
        super().__init__(daemon=True, name="mailsync")
        self._cfg, self._write, self._dir, self._parsers, self._connect = cfg, write, receipts_dir, parsers, connect
        # un seul passage à la fois ; `lock` partagé : aussi entre l'ancienne et la nouvelle relève d'une reconnexion
        self._lock = lock or threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._passed = threading.Event()
        self.last_result: dict | None = None

    def run_once(self) -> dict:
        with self._lock:
            # relève arrêtée (déconnexion, remplacement) pendant l'attente du verrou : aucun passage de plus
            # avec les anciens identifiants
            if self._stopping.is_set():
                return {"messages": 0, "files": 0, "error": None}
            return sync_once(self._cfg, self._write, self._dir, self._parsers, self._connect)

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.last_result = self.run_once()
            except Exception as exc:        # noqa: BLE001 - le fil de fond ne doit jamais mourir
                log.warning("relève des tickets interrompue : %s", type(exc).__name__)
            self._passed.set()
            self._wake.wait(self._cfg.interval_min * 60)
            self._wake.clear()

    def trigger(self) -> None:
        self._wake.set()

    def wait_pass(self, timeout: float) -> bool:
        """Attend la fin d'un passage (pour les tests) ; consomme le signal."""
        done = self._passed.wait(timeout)
        self._passed.clear()
        return done

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
