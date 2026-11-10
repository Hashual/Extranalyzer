import sqlite3
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    """Ouvre la base ; crée le schéma si user_version vaut 0."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        schema = resources.files("app.store").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
    return conn
