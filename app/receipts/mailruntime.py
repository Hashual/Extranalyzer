"""Relève Gmail dans l'application : identifiants du serveur ou saisis dans l'onglet Connexions.

Source des identifiants : 'server' (variables d'environnement, prioritaires), 'app' (fichier `gmail.json` du
volume de données, droits 600, écrit seulement après un test de connexion réussi) ou None (relève désactivée).
La relève démarre, change ou s'arrête sans redémarrage ; un verrou de passage commun à toutes les relèves
successives garantit qu'aucun passage ne chevauche un autre, même pendant une reconnexion.

Le mot de passe d'application n'est jamais renvoyé, journalisé ni laissé dans un fichier temporaire.
"""
import imaplib
import json
import logging
import os
import re
import ssl
import threading
from collections.abc import Callable
from pathlib import Path

from app.config import DEFAULT_MAIL_INTERVAL_MIN, DEFAULT_RECEIPT_LABEL_PAIRS, MailSettings
from app.receipts.mailsync import HOST, PORT, TIMEOUT_S, MailSyncThread

log = logging.getLogger(__name__)

# Fabrique d'une relève : (identifiants, verrou de passage commun) → fil de relève (non démarré).
MakeSyncer = Callable[[MailSettings, "threading.Lock"], MailSyncThread]

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_PASSWORD_LEN = 128
JOIN_TIMEOUT_S = 10.0       # au-delà, l'ancien fil finit seul son passage (le verrou commun empêche tout chevauchement)


class MailNotConfigured(RuntimeError):
    pass


class MailManagedByServer(RuntimeError):
    """Les identifiants viennent de la configuration du serveur : ils ne se changent pas depuis l'interface."""


class MailCredentialsInvalid(ValueError):
    """Adresse ou mot de passe mal formés, ou refusés par Gmail (message destiné à l'utilisateur)."""


class MailUnreachable(RuntimeError):
    """Gmail injoignable (réseau, TLS, délai) : rien n'est enregistré, l'utilisateur peut réessayer."""


def mask_address(address: str | None) -> str | None:
    """`lo…@gmail.com` : deux premiers caractères seulement de la partie locale."""
    if not address:
        return None
    local, _, domain = address.partition("@")
    return f"{local[:2] if len(local) > 2 else local[:1]}…@{domain}"


def normalize(user: str, app_password: str) -> tuple[str, str]:
    """Adresse sans espaces autour, mot de passe sans aucun espace (Google l'affiche par groupes de 4)."""
    address = (user or "").strip()
    if not EMAIL_PATTERN.fullmatch(address):
        raise MailCredentialsInvalid("adresse Gmail invalide (exemple : votre.adresse@gmail.com)")
    password = "".join((app_password or "").split())
    if not password:
        raise MailCredentialsInvalid("collez le mot de passe d'application (16 lettres)")
    if len(password) > MAX_PASSWORD_LEN or not password.isprintable():
        raise MailCredentialsInvalid("mot de passe d'application invalide : collez les 16 lettres données par Google")
    return address, password


def check_login(user: str, app_password: str, connect=imaplib.IMAP4_SSL) -> None:
    """Connexion IMAP de test : login puis ouverture d'INBOX en lecture seule (aucun message lu ni modifié)."""
    try:
        imap = connect(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error):
        raise MailUnreachable("Gmail injoignable : vérifiez la connexion Internet du serveur, puis réessayez") \
            from None
    try:
        try:
            imap.login(user, app_password)
        except imaplib.IMAP4.abort:
            raise MailUnreachable("connexion à Gmail interrompue : réessayez") from None
        except imaplib.IMAP4.error:
            raise MailCredentialsInvalid(
                "Gmail a refusé la connexion : vérifiez l'adresse et le mot de passe d'application (la validation "
                "en deux étapes doit être activée)") from None
        typ, _ = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise MailCredentialsInvalid("Gmail a refusé l'ouverture de la boîte de réception (IMAP désactivé ?)")
    except (OSError, ssl.SSLError, imaplib.IMAP4.error):
        raise MailUnreachable("connexion à Gmail interrompue : réessayez") from None
    finally:
        try:
            imap.logout()
        except Exception:       # noqa: BLE001 - la fermeture ne doit pas masquer le résultat du test
            pass


def _read_file(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return normalize(data["user"], data["app_password"])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        log.warning("identifiants Gmail enregistrés illisibles (%s)", type(exc).__name__)
        return None


def _write_file(path: Path, user: str, app_password: str) -> None:
    """Écriture atomique en 600 ; aucune copie ne reste derrière un échec."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"user": user, "app_password": app_password}, handle)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


class MailRuntime:
    """Identifiants Gmail courants et fil de relève associé (au plus un)."""

    def __init__(self, server: MailSettings | None, make_syncer: MakeSyncer, cred_file: Path,
                 labels: tuple[tuple[str, str], ...] = DEFAULT_RECEIPT_LABEL_PAIRS,
                 interval_min: int = DEFAULT_MAIL_INTERVAL_MIN, connect=imaplib.IMAP4_SSL):
        self._make, self._file, self._labels, self._interval = make_syncer, cred_file, labels, interval_min
        self._connect = connect
        self._pass_lock = threading.Lock()      # commun à toutes les relèves successives
        self._config_lock = threading.Lock()    # une connexion / déconnexion à la fois
        self._run_threads = False
        self.settings: MailSettings | None = None
        self.source: str | None = None          # 'server' | 'app' | None
        self.syncer: MailSyncThread | None = None   # existe dès que configuré (« Relever maintenant »)
        self.thread: MailSyncThread | None = None   # fil de fond démarré, le cas échéant
        if server is not None:
            self._activate(server, "server")
        else:
            stored = _read_file(cred_file)
            if stored is not None:
                self._activate(self._settings(*stored), "app")

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    @property
    def address(self) -> str | None:
        return mask_address(self.settings.user) if self.settings is not None else None

    @property
    def labels(self) -> tuple[tuple[str, str], ...]:
        return self.settings.labels if self.settings is not None else self._labels

    def _settings(self, user: str, app_password: str) -> MailSettings:
        return MailSettings(user=user, app_password=app_password, labels=self._labels, interval_min=self._interval)

    def _activate(self, settings: MailSettings | None, source: str | None) -> None:
        """Remplace la relève courante (arrêtée et attendue avant d'en démarrer une autre)."""
        self._stop_thread()
        self.settings, self.source = settings, source
        self.syncer = self._make(settings, self._pass_lock) if settings is not None else None
        if self.syncer is not None and self._run_threads:
            self.syncer.start()
            self.thread = self.syncer

    def _stop_thread(self) -> None:
        old, self.thread = self.thread, None
        if old is not None:
            old.stop()
            old.join(JOIN_TIMEOUT_S)
            if old.is_alive():
                log.warning("l'ancienne relève des tickets termine son passage en cours")

    def start(self) -> None:
        """Autorise le fil de fond et le démarre si des identifiants sont connus (jamais dans les tests)."""
        with self._config_lock:
            self._run_threads = True
            if self.syncer is not None and self.thread is None:
                self.syncer.start()
                self.thread = self.syncer

    def stop(self) -> None:
        with self._config_lock:
            self._run_threads = False
            self._stop_thread()

    def connect(self, user: str, app_password: str) -> None:
        """Teste les identifiants auprès de Gmail, puis les enregistre et (re)démarre la relève.

        Lève MailManagedByServer, MailCredentialsInvalid (forme ou refus de Gmail) ou MailUnreachable ;
        dans ces cas rien n'est enregistré et la relève en cours est conservée.
        """
        if self.source == "server":
            raise MailManagedByServer("la relève Gmail est gérée par la configuration du serveur")
        address, password = normalize(user, app_password)
        check_login(address, password, self._connect)
        with self._config_lock:
            if self.source == "server":
                raise MailManagedByServer("la relève Gmail est gérée par la configuration du serveur")
            _write_file(self._file, address, password)
            self._activate(self._settings(address, password), "app")
        log.info("identifiants Gmail enregistrés depuis l'interface")

    def disconnect(self) -> None:
        """Arrête la relève et supprime les identifiants enregistrés (les tickets déjà relevés restent)."""
        with self._config_lock:
            if self.source == "server":
                raise MailManagedByServer("la relève Gmail est gérée par la configuration du serveur")
            self._activate(None, None)
            self._file.unlink(missing_ok=True)
        log.info("identifiants Gmail supprimés depuis l'interface")

    def run_once(self) -> dict:
        """Un passage immédiat (« Relever maintenant ») ; jamais en même temps qu'un passage de fond."""
        syncer = self.syncer
        if syncer is None:
            raise MailNotConfigured("relève des tickets désactivée")
        return syncer.run_once()
