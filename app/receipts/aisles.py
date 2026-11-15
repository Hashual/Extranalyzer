"""Rayons (propres à chaque enseigne) et familles de rayons.

Le seed ne contient que des noms de rayons génériques ; il ne remplace jamais la famille d'un rayon
déjà connu (l'utilisateur a pu la choisir).
"""
import sqlite3
import tomllib
from pathlib import Path

from app.store.repo import fold

SEED = Path(__file__).with_name("aisles.seed.toml")


def normalize_aisle(name: str) -> str:
    """Majuscules sans accents, espaces fusionnés, bords retirés."""
    return " ".join(fold(name).split())


def load_aisle_seed(conn: sqlite3.Connection, path: Path) -> None:
    """Familles (idempotent, dans l'ordre du fichier), puis rayons absents. Ne valide pas."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for position, family in enumerate(data.get("family", [])):
        conn.execute("INSERT INTO aisle_families(name, position) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
                     (family["name"], position))
    families = dict(conn.execute("SELECT name, id FROM aisle_families"))
    for store, aisles in data.get("aisles", {}).items():
        for name, family in aisles.items():
            if family not in families:
                raise ValueError(f"famille inconnue dans le seed des rayons : {family}")
            conn.execute("INSERT INTO aisles(store, name, family_id) VALUES (?, ?, ?) "
                         "ON CONFLICT(store, name) DO NOTHING", (store, normalize_aisle(name), families[family]))


def aisle_id(conn: sqlite3.Connection, store: str, name: str) -> int:
    """Identifiant du rayon ; un rayon inconnu est créé sans famille (« à ranger »)."""
    name = normalize_aisle(name)
    row = conn.execute("SELECT id FROM aisles WHERE store=? AND name=?", (store, name)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO aisles(store, name) VALUES (?, ?)", (store, name)).lastrowid
