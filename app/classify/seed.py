"""Chargement idempotent de l'arbre de catégories et des règles génériques."""
import sqlite3
import tomllib
from pathlib import Path

from app.classify.rules import default_priority


def _ensure_category(conn: sqlite3.Connection, name: str, parent_id: int | None, kind: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name = ? AND COALESCE(parent_id, 0) = ?",
                       (name, parent_id or 0)).fetchone()
    if row is not None:
        return row[0]
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, ?, ?)",
                        (name, parent_id, kind)).lastrowid


def _category_by_path(conn: sqlite3.Connection, path: str) -> int:
    parent_id = None
    for name in path.split("/"):
        row = conn.execute("SELECT id FROM categories WHERE name = ? AND COALESCE(parent_id, 0) = ?",
                           (name, parent_id or 0)).fetchone()
        if row is None:
            raise ValueError(f"catégorie inconnue dans le seed : {path}")
        parent_id = row[0]
    return parent_id


def _load(conn: sqlite3.Connection, data: dict, with_rules: bool) -> None:
    for root in data.get("category", []):
        root_id = _ensure_category(conn, root["name"], None, root["kind"])
        for child in root.get("children", []):
            _ensure_category(conn, child, root_id, root["kind"])
    for rule in data.get("rule", []) if with_rules else []:
        category_id = _category_by_path(conn, rule["category"])
        match, direction = rule.get("match", "contains"), rule.get("direction")
        exists = conn.execute(
            "SELECT 1 FROM rules WHERE pattern = ? AND match = ? AND direction IS ? AND account_id IS NULL "
            "AND category_id = ?", (rule["pattern"], match, direction, category_id)).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO rules(pattern, match, direction, account_id, category_id, priority) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (rule["pattern"], match, direction, category_id,
                 default_priority(rule["pattern"], match, direction, None)))


def is_fresh_install(conn: sqlite3.Connection) -> bool:
    """Ni règle ni transaction : seule situation où les règles du seed sont (ré)installées."""
    return conn.execute("SELECT NOT EXISTS (SELECT 1 FROM rules) AND NOT EXISTS (SELECT 1 FROM transactions)"
                        ).fetchone()[0] == 1


def load_seed(conn: sqlite3.Connection, path: Path, *, with_rules: bool = True) -> None:
    """Crée les catégories manquantes et, si `with_rules`, les règles manquantes.

    Valide seulement si aucune transaction SQLite n'est en cours.
    """
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if conn.in_transaction:
        _load(conn, data, with_rules)
    else:
        with conn:
            _load(conn, data, with_rules)
