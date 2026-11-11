"""Configuration lue dans les variables d'environnement."""
import os
from dataclasses import dataclass
from pathlib import Path

from app.auth import hash_password


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    password_hash: bytes          # sel || scrypt, voir app.auth.hash_password
    max_upload_mb: int = 20
    auto_threshold: float = 0.90  # seuil du classement automatique par le modèle


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


def load_settings() -> Settings:
    return Settings(data_dir=Path(os.environ.get("EXTRANALYZER_DATA_DIR", "/data")),
                    password_hash=hash_password(_password()),
                    max_upload_mb=_max_upload_mb(),
                    auto_threshold=auto_threshold())
