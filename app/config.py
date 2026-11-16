"""Configuration lue dans les variables d'environnement."""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.auth import hash_password


log = logging.getLogger(__name__)

DEFAULT_BANK_KEY_FILE = "/run/secrets/enablebanking_key"
DEFAULT_SYNC_HOURS = 12
DEFAULT_HELIUS_KEY_FILE = "/run/secrets/helius_key"
DEFAULT_HELIUS_URL = "https://mainnet.helius-rpc.com"


class ConfigError(RuntimeError):
    pass


DEFAULT_RECEIPT_LABELS = "Super U/Tickets de caisses=superu;intermarche/Tickets de caisses=intermarche"
DEFAULT_MAIL_INTERVAL_MIN = 60


def parse_receipt_labels(raw: str) -> tuple[tuple[str, str], ...]:
    """`libellé=enseigne;libellé=enseigne` → ((libellé, enseigne), ...)."""
    pairs = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        label, sep, store = item.rpartition("=")
        if not sep or not label.strip() or not store.strip():
            raise ConfigError(f"EXTRANALYZER_RECEIPT_LABELS invalide : {item!r} (format libellé=enseigne)")
        pairs.append((label.strip(), store.strip()))
    return tuple(pairs)


DEFAULT_RECEIPT_LABEL_PAIRS = parse_receipt_labels(DEFAULT_RECEIPT_LABELS)


@dataclass(frozen=True)
class MailSettings:
    user: str = field(repr=False)           # hors repr : ni adresse ni secret dans les journaux
    app_password: str = field(repr=False)   # mot de passe d'application Gmail, sans espaces
    labels: tuple[tuple[str, str], ...]     # (libellé Gmail, enseigne)
    interval_min: int = 60


@dataclass(frozen=True)
class BankSettings:
    """Connexion Enable Banking. La clé privée vient d'un secret Docker ; elle n'est jamais écrite en base."""
    app_id: str
    key_pem: bytes = field(repr=False)
    redirect_url: str
    aspsp_name: str = "Crédit Mutuel"
    country: str = "FR"


@dataclass(frozen=True)
class SolanaSettings:
    """Clé Helius fournie par le serveur (secret Docker). Jamais écrite en base, journalisée ni renvoyée par l'API."""
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_HELIUS_URL


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password_hash: bytes          # sel || scrypt, voir app.auth.hash_password
    max_upload_mb: int = 20
    auto_threshold: float = 0.90  # seuil du classement automatique par le modèle
    mail: MailSettings | None = None    # identifiants Gmail du serveur ; None : saisis dans l'onglet Connexions
    mail_labels: tuple[tuple[str, str], ...] = DEFAULT_RECEIPT_LABEL_PAIRS   # libellés relevés (toutes sources)
    mail_interval_min: int = DEFAULT_MAIL_INTERVAL_MIN
    bank: BankSettings | None = None   # None : récupération bancaire désactivée
    sync_hours: int = 0           # période de la synchro planifiée (0 : désactivée)
    solana: SolanaSettings | None = None   # clé Helius du serveur ; None : clé saisie dans l'interface
    helius_url: str | None = DEFAULT_HELIUS_URL   # None : adresse invalide, portefeuilles désactivés


def _password() -> str:
    direct = os.environ.get("EXTRANALYZER_PASSWORD")
    file = os.environ.get("EXTRANALYZER_PASSWORD_FILE")
    if direct and file:
        raise ConfigError("définir EXTRANALYZER_PASSWORD ou EXTRANALYZER_PASSWORD_FILE, pas les deux")
    if file:
        try:
            direct = Path(file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"lecture impossible de EXTRANALYZER_PASSWORD_FILE : {exc}") from exc
    if not direct:
        raise ConfigError("mot de passe absent : définir EXTRANALYZER_PASSWORD ou EXTRANALYZER_PASSWORD_FILE")
    return direct


def _max_upload_mb() -> int:
    raw = os.environ.get("EXTRANALYZER_MAX_UPLOAD_MB", "20")
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"EXTRANALYZER_MAX_UPLOAD_MB invalide : {raw!r}") from None
    if value <= 0:
        raise ConfigError("EXTRANALYZER_MAX_UPLOAD_MB doit être positif")
    return value


DEFAULT_AUTO_THRESHOLD = 0.90


def auto_threshold() -> float:
    """Seuil de confiance du classement automatique : EXTRANALYZER_AUTO_THRESHOLD, dans ]0, 1]."""
    raw = os.environ.get("EXTRANALYZER_AUTO_THRESHOLD")
    if raw is None:
        return DEFAULT_AUTO_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"EXTRANALYZER_AUTO_THRESHOLD invalide : {raw!r}") from None
    if not 0 < value <= 1:          # écarte aussi nan
        raise ConfigError("EXTRANALYZER_AUTO_THRESHOLD doit être dans ]0, 1]")
    return value


def _gmail_password() -> str:
    direct = os.environ.get("EXTRANALYZER_GMAIL_APP_PASSWORD")
    file = os.environ.get("EXTRANALYZER_GMAIL_APP_PASSWORD_FILE")
    if direct and file:
        raise ConfigError("définir EXTRANALYZER_GMAIL_APP_PASSWORD ou EXTRANALYZER_GMAIL_APP_PASSWORD_FILE, "
                          "pas les deux")
    if file:
        try:
            direct = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"lecture impossible de EXTRANALYZER_GMAIL_APP_PASSWORD_FILE : {exc}") from exc
    return "".join((direct or "").split())      # Google affiche le mot de passe par groupes de 4 séparés d'espaces


def _receipt_labels() -> tuple[tuple[str, str], ...]:
    return parse_receipt_labels(os.environ.get("EXTRANALYZER_RECEIPT_LABELS", DEFAULT_RECEIPT_LABELS))


def _mail_interval_min() -> int:
    raw = os.environ.get("EXTRANALYZER_MAIL_INTERVAL_MIN", str(DEFAULT_MAIL_INTERVAL_MIN))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"EXTRANALYZER_MAIL_INTERVAL_MIN invalide : {raw!r}") from None
    if value <= 0:
        raise ConfigError("EXTRANALYZER_MAIL_INTERVAL_MIN doit être positif")
    return value


def mail_settings() -> MailSettings | None:
    """Relève Gmail des tickets de caisse ; None (désactivée) sans utilisateur ou sans mot de passe."""
    password = _gmail_password()
    user = os.environ.get("EXTRANALYZER_GMAIL_USER", "").strip()
    if not user or not password:
        return None
    return MailSettings(user=user, app_password=password, labels=_receipt_labels(),
                        interval_min=_mail_interval_min())


def _bank_settings() -> BankSettings | None:
    """Enable Banking : activé si EXTRANALYZER_EB_APP_ID est défini, la clé lisible et l'URL de retour fournie ;
    sinon désactivé (l'appli démarre, l'onglet Connexions explique quoi configurer)."""
    app_id = os.environ.get("EXTRANALYZER_EB_APP_ID", "").strip()
    if not app_id:
        return None
    redirect_url = os.environ.get("EXTRANALYZER_EB_REDIRECT_URL", "").strip()
    if not redirect_url.startswith(("http://", "https://")):
        log.warning("Enable Banking désactivé : EXTRANALYZER_EB_REDIRECT_URL absente ou invalide")
        return None
    key_file = Path(os.environ.get("EXTRANALYZER_EB_KEY_FILE", DEFAULT_BANK_KEY_FILE))
    try:
        key_pem = key_file.read_bytes()
    except OSError as exc:
        log.warning("Enable Banking désactivé : clé privée illisible (%s)", type(exc).__name__)
        return None
    aspsp = os.environ.get("EXTRANALYZER_EB_ASPSP", "").strip() or "Crédit Mutuel"
    return BankSettings(app_id=app_id, key_pem=key_pem, redirect_url=redirect_url, aspsp_name=aspsp)


def _helius_url() -> str | None:
    """Adresse du service Helius : EXTRANALYZER_HELIUS_URL (https obligatoire), sinon l'adresse par défaut."""
    base_url = os.environ.get("EXTRANALYZER_HELIUS_URL", "").strip() or DEFAULT_HELIUS_URL
    if not base_url.startswith("https://"):
        log.warning("Helius désactivé : EXTRANALYZER_HELIUS_URL doit commencer par https://")
        return None
    return base_url


def _solana_settings() -> SolanaSettings | None:
    """Clé Helius fournie par la configuration du serveur (secret Docker), facultative : sans elle, la clé se saisit
    dans l'onglet Connexions. Un fichier absent au chemin par défaut n'est pas une erreur."""
    base_url = _helius_url()
    if base_url is None:
        return None
    explicit = os.environ.get("EXTRANALYZER_HELIUS_KEY_FILE")
    key_file = Path(explicit or DEFAULT_HELIUS_KEY_FILE)
    if not explicit and not key_file.exists():
        return None
    try:
        api_key = key_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        log.warning("clé Helius du serveur illisible (%s)", type(exc).__name__)
        return None
    if not api_key:
        log.warning("clé Helius du serveur vide")
        return None
    return SolanaSettings(api_key=api_key, base_url=base_url)


def _sync_hours() -> int:
    raw = os.environ.get("EXTRANALYZER_SYNC_HOURS", str(DEFAULT_SYNC_HOURS))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"EXTRANALYZER_SYNC_HOURS invalide : {raw!r}") from None
    if value < 0:
        raise ConfigError("EXTRANALYZER_SYNC_HOURS doit être positif ou nul")
    return value


def load_settings() -> Settings:
    return Settings(data_dir=Path(os.environ.get("EXTRANALYZER_DATA_DIR", "/data")),
                    password_hash=hash_password(_password()),
                    max_upload_mb=_max_upload_mb(),
                    auto_threshold=auto_threshold(),
                    mail=mail_settings(),
                    mail_labels=_receipt_labels(),
                    mail_interval_min=_mail_interval_min(),
                    bank=_bank_settings(),
                    sync_hours=_sync_hours(),
                    solana=_solana_settings(),
                    helius_url=_helius_url())
