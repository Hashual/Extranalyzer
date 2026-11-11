"""Sous-catégorie d'Épargne associée à un compte d'épargne. Ne valide pas : l'appelant possède la transaction."""
import re
import sqlite3

from app.store.repo import fold

ROOT_NAME = "Épargne"
_ALIASES = {"EPARGNE LOGEMENT": "CEL"}


def _savings_root(conn: sqlite3.Connection) -> int:
    for cat_id, name in conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY id"):
        if fold(name) == fold(ROOT_NAME):
            return cat_id
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, NULL, 'savings')",
                        (ROOT_NAME,)).lastrowid


def _has_words(norm_label: str, words: str) -> bool:
    """`words` apparaît en mots entiers dans le libellé (« CEL » ne reconnaît pas « MARCEL »)."""
    return re.search(rf"\b{re.escape(fold(words))}\b", norm_label) is not None


def savings_category_for_account(conn: sqlite3.Connection, account_id: int) -> int:
    """Sous-catégorie d'Épargne du compte : retrouvée ou créée, idempotente."""
    label = conn.execute("SELECT label FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
    norm = fold(label)
    alias = next((target for key, target in _ALIASES.items() if _has_words(norm, key)), None)
    root = _savings_root(conn)
    children = conn.execute("SELECT id, name FROM categories WHERE parent_id = ?", (root,)).fetchall()
    if alias is not None:
        for cat_id, name in children:
            if fold(name) == fold(alias):
                return cat_id
        name = alias
    else:
        found = [(len(fold(n)), i) for i, n in children if _has_words(norm, n)]
        if found:
            return max(found)[1]
        name = label.title()
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, ?, 'savings')",
                        (name, root)).lastrowid
