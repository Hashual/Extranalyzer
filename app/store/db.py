import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 2
BUSY_TIMEOUT_S = 30     # une écriture d'onglet attend la fin d'une ré-analyse au lieu d'échouer


def connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Ouvre la base ; crée le schéma si user_version vaut 0, migre une base plus ancienne.

    `check_same_thread=False` : connexion ouverte dans un thread et utilisée (séquentiellement) dans un
    autre, comme une dépendance FastAPI par requête.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"schéma de base inconnu (user_version={version} > {SCHEMA_VERSION})")
    if version == 0:
        schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
    elif version == 1:      # 1 -> 2 : message persistant d'une section (ex. collision d'identités)
        conn.executescript("BEGIN; ALTER TABLE sections ADD COLUMN message TEXT; PRAGMA user_version = 2; COMMIT;")
    return conn
