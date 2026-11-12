"""Configuration lue dans les variables d'environnement."""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.auth import hash_password


log = logging.getLogger(__name__)

DEFAULT_BANK_KEY_FILE = "/run/secrets/enablebanking_key"
DEFAULT_SYNC_HOURS = 12


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class BankSettings:
    """Connexion Enable Banking. La clé privée vient d'un secret Docker ; elle n'est jamais écrite en base."""
    app_id: str
    key_pem: bytes = field(repr=False)
    redirect_url: str
    aspsp_name: str = "Crédit Mutuel"
    country: str = "FR"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password_hash: bytes          # sel || scrypt, voir app.auth.hash_password
    max_upload_mb: int = 20
    auto_threshold: float = 0.90  # seuil du classement automatique par le modèle
    bank: BankSettings | None = None   # None : récupération bancaire désactivée
    sync_hours: int = 0           # période de la synchro planifiée (0 : désactivée)


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


def _bank_settings() -> BankSettings | None:
    """Enable Banking : activé si EXTRANALYZER_EB_APP_ID est défini, la clé lisible et l'URL de retour fournie ;
    sinon désactivé (l'appli démarre, l'onglet Banque explique quoi configurer)."""
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
                    bank=_bank_settings(),
                    sync_hours=_sync_hours())
