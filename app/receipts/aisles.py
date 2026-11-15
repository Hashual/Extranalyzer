"""Rayons (propres à chaque enseigne) et familles de rayons.

Le seed ne contient que des noms de rayons génériques ; il ne remplace jamais la famille d'un rayon
déjà connu (l'utilisateur a pu la choisir) et ne recrée pas une famille renommée ou supprimée.
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
    """Complète familles et rayons depuis le seed, sans jamais défaire un choix de l'utilisateur. Ne valide pas.

    Les familles ne sont créées que si la table est vide (première installation) : une famille renommée ou
    supprimée ne revient pas. Un rayon du seed n'est ajouté que s'il est absent et que sa famille existe
    encore sous ce nom. Un rayon rangé dans une famille non déclarée par le fichier est une erreur du seed.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    declared = {family["name"] for family in data.get("family", [])}
    for aisles in data.get("aisles", {}).values():
        for family in aisles.values():
            if family not in declared:
                raise ValueError(f"famille inconnue dans le seed des rayons : {family}")
    if conn.execute("SELECT 1 FROM aisle_families LIMIT 1").fetchone() is None:
        conn.executemany("INSERT INTO aisle_families(name, position) VALUES (?, ?)",
                         [(family["name"], position) for position, family in enumerate(data.get("family", []))])
    families = dict(conn.execute("SELECT name, id FROM aisle_families"))
    for store, aisles in data.get("aisles", {}).items():
        for name, family in aisles.items():
            if family in families:
                conn.execute("INSERT INTO aisles(store, name, family_id) VALUES (?, ?, ?) "
                             "ON CONFLICT(store, name) DO NOTHING", (store, normalize_aisle(name), families[family]))


def aisle_id(conn: sqlite3.Connection, store: str, name: str) -> int:
    """Identifiant du rayon ; un rayon inconnu est créé sans famille (« à ranger »)."""
    name = normalize_aisle(name)
    row = conn.execute("SELECT id FROM aisles WHERE store=? AND name=?", (store, name)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO aisles(store, name) VALUES (?, ?)", (store, name)).lastrowid
