"""Gestion des catégories par l'utilisateur : création, renommage, suppression. Ne valide pas : l'appelant
possède la transaction.

Hiérarchie à deux niveaux : une sous-catégorie se crée sous une racine et hérite de son `kind`.

Suppression (choix le plus sûr : rien n'est jamais perdu ni laissé orphelin) :
- une catégorie qui a des sous-catégories est refusée tant qu'elle en a ;
- une catégorie utilisée (opérations, règles ou reclassements manuels) n'est supprimée que si l'appelant désigne
  une catégorie de destination du même `kind` : tout y est déplacé (les reclassements manuels restent manuels),
  puis la chaîne de classement repasse. Sans destination, la suppression est refusée avec le détail des usages ;
- une catégorie inutilisée se supprime directement (ses éventuelles suggestions sont oubliées puis recalculées).

Une catégorie du seed supprimée ou renommée est mémorisée pour que le seed ne la recrée pas au démarrage
(`app.classify.seed`). Limite connue : les sous-catégories d'Épargne d'un compte d'épargne sont dérivées de
l'appariement des virements internes (`app.classify.savings_target`) ; supprimée ou renommée, une telle
sous-catégorie est recréée sous son nom d'origine tant que des virements appariés non manuels y mènent.
"""
import sqlite3
import unicodedata
from dataclasses import dataclass

from app.classify.pipeline import reclassify
from app.classify.seed import dismiss_seed_category, restore_seed_category
from app.store.repo import fold

KINDS = ("expense", "income", "savings")
NAME_MAX = 60


class CategoryNotFound(LookupError):
    pass


class CategoryConflict(Exception):
    """Opération refusée en l'état (doublon, sous-catégories, usages sans destination) : HTTP 409."""


@dataclass(frozen=True)
class Usage:
    transactions: int      # opérations non miroir classées dans la catégorie
    rules: int
    overrides: int         # reclassements manuels mémorisés (manual_overrides)

    @property
    def any(self) -> bool:
        return bool(self.transactions or self.rules or self.overrides)


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n <= 1 else plural or word + 's'}"


def clean_name(name: str) -> str:
    """Nom sans espaces superflus ; ValueError s'il est vide, trop long ou contient un caractère de contrôle."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("le nom de la catégorie est vide")
    if len(name) > NAME_MAX:
        raise ValueError(f"le nom de la catégorie dépasse {NAME_MAX} caractères")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise ValueError("le nom de la catégorie contient un caractère non imprimable")
    return name


def _get(conn: sqlite3.Connection, category_id: int) -> tuple[str, int | None, str]:
    row = conn.execute("SELECT name, parent_id, kind FROM categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        raise CategoryNotFound(f"catégorie {category_id} introuvable")
    return row


def _name_of(conn: sqlite3.Connection, category_id: int | None) -> str | None:
    if category_id is None:
        return None
    return conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()[0]


def _check_unique(conn: sqlite3.Connection, name: str, parent_id: int | None, exclude: int | None = None) -> None:
    """Pas deux catégories sœurs de même nom, à la casse et aux accents près (« Sante » / « Santé »)."""
    for other_id, other in conn.execute("SELECT id, name FROM categories WHERE parent_id IS ?", (parent_id,)):
        if other_id != exclude and fold(other) == fold(name):
            where = f"sous « {_name_of(conn, parent_id)} »" if parent_id is not None else "parmi les racines"
            raise CategoryConflict(f"une catégorie « {other} » existe déjà {where}")


def create_category(conn: sqlite3.Connection, name: str, parent_id: int | None = None,
                    kind: str | None = None) -> int:
    """Sous-catégorie d'une racine (hérite de son `kind`) ou nouvelle racine (`kind` obligatoire)."""
    name = clean_name(name)
    if parent_id is not None:
        _, grand_parent, kind = _get(conn, parent_id)
        if grand_parent is not None:
            raise ValueError("une sous-catégorie ne peut pas avoir de sous-catégorie (deux niveaux au plus)")
    elif kind not in KINDS:
        raise ValueError("une catégorie racine doit avoir un type : dépense, revenu ou épargne")
    _check_unique(conn, name, parent_id)
    try:
        category_id = conn.execute("INSERT INTO categories(name, parent_id, kind) VALUES (?, ?, ?)",
                                   (name, parent_id, kind)).lastrowid
    except sqlite3.IntegrityError:
        raise CategoryConflict(f"la catégorie « {name} » existe déjà") from None
    restore_seed_category(conn, name, _name_of(conn, parent_id))
    return category_id


def rename_category(conn: sqlite3.Connection, category_id: int, name: str) -> None:
    old, parent_id, _ = _get(conn, category_id)
    name = clean_name(name)
    if name == old:
        return
    _check_unique(conn, name, parent_id, exclude=category_id)
    try:
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
    except sqlite3.IntegrityError:
        raise CategoryConflict(f"la catégorie « {name} » existe déjà") from None
    parent_name = _name_of(conn, parent_id)
    dismiss_seed_category(conn, old, parent_name)
    restore_seed_category(conn, name, parent_name)


def usage(conn: sqlite3.Connection, category_id: int) -> Usage:
    one = lambda sql: conn.execute(sql, (category_id,)).fetchone()[0]   # noqa: E731
    return Usage(one("SELECT COUNT(*) FROM transactions WHERE category_id = ? AND mirror_of IS NULL"),
                 one("SELECT COUNT(*) FROM rules WHERE category_id = ?"),
                 one("SELECT COUNT(*) FROM manual_overrides WHERE category_id = ?"))


def describe(u: Usage) -> str:
    parts = [_plural(u.transactions, "opération"), _plural(u.rules, "règle"),
             _plural(u.overrides, "reclassement manuel", "reclassements manuels")]
    return ", ".join(parts[:-1]) + " et " + parts[-1]


def delete_category(conn: sqlite3.Connection, category_id: int, move_to: int | None = None) -> Usage:
    """Supprime la catégorie (voir les règles en tête de module) ; renvoie ce qui a été déplacé vers `move_to`."""
    name, parent_id, kind = _get(conn, category_id)
    children = conn.execute("SELECT COUNT(*) FROM categories WHERE parent_id = ?", (category_id,)).fetchone()[0]
    if children:
        raise CategoryConflict(f"« {name} » a encore {_plural(children, 'sous-catégorie')} : "
                               "supprimez-les d'abord")
    used = usage(conn, category_id)
    if move_to is not None:
        if move_to == category_id:
            raise ValueError("la catégorie de destination doit être différente de celle supprimée")
        target_name, _, target_kind = _get(conn, move_to)
        if target_kind != kind:
            raise ValueError(f"« {target_name} » n'est pas du même type que « {name} »")
    elif used.any:
        raise CategoryConflict(f"« {name} » est utilisée ({describe(used)}) : choisissez une catégorie "
                               "vers laquelle les déplacer")
    if move_to is not None:
        for table in ("transactions", "rules", "manual_overrides"):
            conn.execute(f"UPDATE {table} SET category_id = ? WHERE category_id = ?", (move_to, category_id))
    conn.execute("UPDATE transactions SET suggested_category_id = NULL, suggestion_reason = NULL, "
                 "suggestion_support = NULL, confidence = CASE WHEN classified_by IS NULL THEN NULL "
                 "ELSE confidence END WHERE suggested_category_id = ?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    dismiss_seed_category(conn, name, _name_of(conn, parent_id))
    reclassify(conn)
    return used if move_to is not None else Usage(0, 0, 0)
