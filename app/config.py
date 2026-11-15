"""Configuration lue dans les variables d'environnement."""
import os
from dataclasses import dataclass
from pathlib import Path

from app.auth import hash_password


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class MailSettings:
    user: str
    app_password: str                       # mot de passe d'application Gmail, sans espaces
    labels: tuple[tuple[str, str], ...]     # (libellé Gmail, enseigne)
    interval_min: int = 60


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password_hash: bytes          # sel || scrypt, voir app.auth.hash_password
    max_upload_mb: int = 20
    auto_threshold: float = 0.90  # seuil du classement automatique par le modèle
    mail: MailSettings | None = None    # None : relève des tickets de caisse désactivée


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


DEFAULT_RECEIPT_LABELS = "Super U/Tickets de caisses=superu;intermarche/Tickets de caisses=intermarche"
DEFAULT_MAIL_INTERVAL_MIN = 60


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
    raw = os.environ.get("EXTRANALYZER_RECEIPT_LABELS", DEFAULT_RECEIPT_LABELS)
    pairs = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        label, sep, store = item.rpartition("=")
        if not sep or not label.strip() or not store.strip():
            raise ConfigError(f"EXTRANALYZER_RECEIPT_LABELS invalide : {item!r} (format libellé=enseigne)")
        pairs.append((label.strip(), store.strip()))
    return tuple(pairs)


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


def load_settings() -> Settings:
    return Settings(data_dir=Path(os.environ.get("EXTRANALYZER_DATA_DIR", "/data")),
                    password_hash=hash_password(_password()),
                    max_upload_mb=_max_upload_mb(),
                    auto_threshold=auto_threshold(),
                    mail=mail_settings())
