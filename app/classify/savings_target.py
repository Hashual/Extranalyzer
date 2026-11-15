"""Sous-catégorie d'Épargne associée à un compte d'épargne. Ne valide pas : l'appelant possède la transaction."""
import re
import sqlite3

from app.store.repo import fold

ROOT_NAME = "Épargne"
_ALIASES = {"EPARGNE LOGEMENT": "CEL"}


def _find_savings_root(conn: sqlite3.Connection) -> int | None:
    """Racine « Épargne », sinon l'unique racine d'épargne (« Épargne » renommée par l'utilisateur), sinon None."""
    roots = conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL AND kind = 'savings' "
                         "ORDER BY id").fetchall()
    for cat_id, name in roots:
        if fold(name) == fold(ROOT_NAME):
            return cat_id
    return roots[0][0] if len(roots) == 1 else None


def _savings_root(conn: sqlite3.Connection) -> int:
    """Racine d'épargne, créée si elle n'existe pas."""
    found = _find_savings_root(conn)
    if found is not None:
        return found
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, NULL, 'savings')",
                        (ROOT_NAME,)).lastrowid


def _has_words(norm_label: str, words: str) -> bool:
    """`words` apparaît en mots entiers dans le libellé (« CEL » ne reconnaît pas « MARCEL ») ; des
    lookarounds plutôt que `\\b`, qui échoue quand `words` commence ou finit par une ponctuation (« LIVRET A. »)."""
    return re.search(rf"(?<!\w){re.escape(fold(words))}(?!\w)", norm_label) is not None


def savings_category_named(conn: sqlite3.Connection, name: str) -> int:
    """Sous-catégorie d'Épargne `name` : retrouvée (comparaison `fold`) ou créée, idempotente."""
    root = _savings_root(conn)
    children = conn.execute("SELECT id, name FROM categories WHERE parent_id = ?", (root,)).fetchall()
    same = next((i for i, n in children if fold(n) == fold(name)), None)
    if same is not None:
        return same
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, ?, 'savings')",
                        (name, root)).lastrowid


def savings_category_for_account(conn: sqlite3.Connection, account_id: int, create: bool = True) -> int | None:
    """Sous-catégorie d'Épargne du compte : retrouvée ou créée, idempotente.

    `create=False` : même recherche, mais renvoie None au lieu d'insérer (aucune écriture)."""
    label = conn.execute("SELECT label FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
    norm = fold(label)
    alias = next((target for key, target in _ALIASES.items() if _has_words(norm, key)), None)
    root = _savings_root(conn) if create else _find_savings_root(conn)
    if root is None:
        return None
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
    same = next((i for i, n in children if fold(n) == fold(name)), None)    # jamais de doublon (idempotence)
    if same is not None:
        return same
    if not create:
        return None
    return conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, ?, 'savings')",
                        (name, root)).lastrowid
