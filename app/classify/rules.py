"""Moteur de règles de classement et reclassement manuel.

Aucune fonction ne valide : l'appelant possède la transaction. Les transactions `manual=1` ne sont
jamais modifiées par les règles.
"""
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

MATCHES = ("contains", "startswith", "regex")
DIRECTIONS = (None, "debit", "credit")
_UPDATABLE = {"pattern", "match", "category_id", "direction", "account_id", "priority"}


@dataclass(frozen=True)
class _Rule:
    id: int
    pattern: str
    match: str
    direction: str | None
    account_id: int | None
    category_id: int


def default_priority(pattern: str, match: str, direction: str | None, account_id: int | None) -> int:
    return (10 * len(pattern) + (5 if direction else 0) + (5 if account_id is not None else 0)
            + (3 if match == "startswith" else 0))


def _validate(pattern: str, match: str, direction: str | None) -> None:
    if match not in MATCHES:
        raise ValueError(f"type de correspondance inconnu : {match!r}")
    if direction not in DIRECTIONS:
        raise ValueError(f"sens inconnu : {direction!r}")
    if not pattern:
        raise ValueError("motif vide")
    if match == "regex":
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"expression régulière invalide : {exc}") from exc


def _matches(rule: _Rule, label_norm: str, amount_cents: int, account_id: int) -> bool:
    if rule.account_id is not None and rule.account_id != account_id:
        return False
    if rule.direction == "debit" and amount_cents >= 0:
        return False
    if rule.direction == "credit" and amount_cents <= 0:
        return False
    if rule.match == "regex":
        return re.search(rule.pattern, label_norm, re.IGNORECASE) is not None
    label, pattern = label_norm.casefold(), rule.pattern.casefold()
    return label.startswith(pattern) if rule.match == "startswith" else pattern in label


def _rules_by_precedence(conn: sqlite3.Connection) -> list[_Rule]:
    return [_Rule(*row) for row in conn.execute(
        "SELECT id, pattern, match, direction, account_id, category_id FROM rules "
        "ORDER BY priority DESC, id ASC")]


def _in_clause(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def _non_manual_rows(conn: sqlite3.Connection, tx_ids: list[int] | None) -> list[tuple]:
    sql = "SELECT id, label_norm, amount_cents, account_id FROM transactions WHERE manual = 0"
    if tx_ids is None:
        return conn.execute(sql).fetchall()
    rows = []
    for start in range(0, len(tx_ids), 500):     # reste sous la limite de variables SQLite
        chunk = tx_ids[start:start + 500]
        rows += conn.execute(f"{sql} AND id IN ({_in_clause(chunk)})", chunk).fetchall()
    return rows


def apply_rules(conn: sqlite3.Connection, tx_ids: list[int] | None = None) -> int:
    """Reclasse les transactions non manuelles (toutes si `tx_ids` est None) ; renvoie le nombre classées."""
    rules = _rules_by_precedence(conn)
    classified = 0
    for tx_id, label_norm, amount_cents, account_id in _non_manual_rows(conn, tx_ids):
        winner = next((r for r in rules if _matches(r, label_norm, amount_cents, account_id)), None)
        if winner is None:
            conn.execute("UPDATE transactions SET category_id = NULL, rule_id = NULL WHERE id = ?", (tx_id,))
        else:
            conn.execute("UPDATE transactions SET category_id = ?, rule_id = ? WHERE id = ?",
                         (winner.category_id, winner.id, tx_id))
            classified += 1
    return classified


def create_rule(conn: sqlite3.Connection, pattern: str, match: str, category_id: int,
                direction: str | None = None, account_id: int | None = None, priority: int | None = None,
                apply_to: Literal["unclassified", "all_non_manual"] = "unclassified") -> int:
    if apply_to not in ("unclassified", "all_non_manual"):
        raise ValueError(f"apply_to inconnu : {apply_to!r}")
    _validate(pattern, match, direction)
    if priority is None:
        priority = default_priority(pattern, match, direction, account_id)
    rule_id = conn.execute(
        "INSERT INTO rules(pattern, match, direction, account_id, category_id, priority) "
        "VALUES (?, ?, ?, ?, ?, ?)", (pattern, match, direction, account_id, category_id, priority)).lastrowid
    where = "manual = 0" + (" AND category_id IS NULL" if apply_to == "unclassified" else "")
    apply_rules(conn, [r[0] for r in conn.execute(f"SELECT id FROM transactions WHERE {where}")])
    return rule_id


def update_rule(conn: sqlite3.Connection, rule_id: int, **fields) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"champs inconnus : {sorted(unknown)}")
    row = conn.execute("SELECT pattern, match, direction FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise ValueError(f"règle {rule_id} introuvable")
    merged = dict(zip(("pattern", "match", "direction"), row)) | {
        k: v for k, v in fields.items() if k in ("pattern", "match", "direction")}
    _validate(merged["pattern"], merged["match"], merged["direction"])
    if fields:
        conn.execute(f"UPDATE rules SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), rule_id))
    # Concernées : celles déjà classées par cette règle, les non classées et celles que la règle modifiée reconnaît.
    rule = next(r for r in _rules_by_precedence(conn) if r.id == rule_id)
    ids = [tx_id for tx_id, label_norm, amount, account_id, current_rule, category in conn.execute(
               "SELECT id, label_norm, amount_cents, account_id, rule_id, category_id "
               "FROM transactions WHERE manual = 0")
           if current_rule == rule_id or category is None or _matches(rule, label_norm, amount, account_id)]
    apply_rules(conn, ids)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE rule_id = ? AND manual = 0", (rule_id,))]
    conn.execute("UPDATE transactions SET category_id = NULL, rule_id = NULL WHERE rule_id = ? AND manual = 0",
                 (rule_id,))
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    apply_rules(conn, ids)


def set_manual_category(conn: sqlite3.Connection, tx_id: int, category_id: int | None) -> None:
    row = conn.execute("SELECT tx_key FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        raise ValueError(f"transaction {tx_id} introuvable")
    tx_key = row[0]
    if category_id is None:
        conn.execute("DELETE FROM manual_overrides WHERE tx_key = ?", (tx_key,))
        conn.execute("UPDATE transactions SET manual = 0, category_id = NULL, rule_id = NULL WHERE id = ?",
                     (tx_id,))
        apply_rules(conn, [tx_id])
        return
    conn.execute("INSERT INTO manual_overrides(tx_key, category_id) VALUES (?, ?) "
                 "ON CONFLICT(tx_key) DO UPDATE SET category_id = excluded.category_id", (tx_key, category_id))
    conn.execute("UPDATE transactions SET manual = 1, category_id = ?, rule_id = NULL WHERE id = ?",
                 (category_id, tx_id))
