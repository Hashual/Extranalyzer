"""Onglet « Règles » : liste, création, modification, suppression des règles de classement, et aperçu
des libellés reconnus par un motif.

Créer une règle classe les opérations encore à classer ; la modifier ou la supprimer reclasse les opérations
concernées (sauf les reclassements manuels) — voir `app.classify.rules`.
"""
import sqlite3
from collections import Counter
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

# `_Rule` / `_matches` / `_validate` : l'aperçu doit reconnaître exactement ce que reconnaîtra la règle.
from app.classify.rules import _matches, _Rule, _validate, create_rule, delete_rule, update_rule
from app.deps import get_conn
from app.tabs.registry import Tab

PREVIEW_LIMIT = 20
REQUIRED = ("pattern", "match", "category_id", "priority")     # non annulables lors d'une modification

Match = Literal["contains", "startswith", "regex"]
Direction = Literal["debit", "credit"]

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class RuleBody(BaseModel):
    pattern: str
    match: Match
    category_id: int
    direction: Direction | None = None
    account_id: int | None = None
    priority: int | None = None           # défaut : selon la spécificité du motif


class RulePatch(BaseModel):
    pattern: str | None = None
    match: Match | None = None
    category_id: int | None = None
    direction: Direction | None = None
    account_id: int | None = None
    priority: int | None = None


class PreviewBody(BaseModel):
    pattern: str
    match: Match
    direction: Direction | None = None


RULE_SELECT = """
    SELECT r.id, r.pattern, r.match, r.direction, r.account_id, r.category_id, c.name, p.name, r.priority,
           (SELECT COUNT(*) FROM transactions t WHERE t.rule_id = r.id AND t.mirror_of IS NULL)
    FROM rules r JOIN categories c ON c.id = r.category_id LEFT JOIN categories p ON p.id = c.parent_id
"""


def _rule_dict(row: tuple) -> dict:
    rule_id, pattern, match, direction, account_id, category_id, name, parent, priority, tx_count = row
    return {"id": rule_id, "pattern": pattern, "match": match, "direction": direction, "account_id": account_id,
            "category_id": category_id, "category_path": f"{parent} › {name}" if parent else name,
            "priority": priority, "tx_count": tx_count}


def _get_rule(conn: sqlite3.Connection, rule_id: int) -> dict:
    row = conn.execute(RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"règle {rule_id} introuvable")
    return _rule_dict(row)


def _check_refs(conn: sqlite3.Connection, fields: dict) -> None:
    if "category_id" in fields and conn.execute(
            "SELECT 1 FROM categories WHERE id = ?", (fields["category_id"],)).fetchone() is None:
        raise HTTPException(404, f"catégorie {fields['category_id']} introuvable")
    if fields.get("account_id") is not None and conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (fields["account_id"],)).fetchone() is None:
        raise HTTPException(404, f"compte {fields['account_id']} introuvable")


@router.get("")
def list_rules(conn: Conn):
    return [_rule_dict(row) for row in conn.execute(RULE_SELECT + " ORDER BY r.priority DESC, r.id ASC")]


@router.post("", status_code=201)
def add_rule(body: RuleBody, conn: Conn):
    fields = body.model_dump()
    _check_refs(conn, fields)
    with conn:
        try:
            rule_id = create_rule(conn, body.pattern.strip(), body.match, body.category_id,
                                  direction=body.direction, account_id=body.account_id, priority=body.priority)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    return _get_rule(conn, rule_id)


@router.patch("/{rule_id}")
def edit_rule(rule_id: int, body: RulePatch, conn: Conn):
    _get_rule(conn, rule_id)
    fields = body.model_dump(exclude_unset=True)
    nulls = [key for key in REQUIRED if key in fields and fields[key] is None]
    if nulls:
        raise HTTPException(422, f"champs obligatoires : {', '.join(nulls)}")
    if "pattern" in fields:
        fields["pattern"] = fields["pattern"].strip()
    _check_refs(conn, fields)
    with conn:
        try:
            update_rule(conn, rule_id, **fields)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    return _get_rule(conn, rule_id)


@router.delete("/{rule_id}", status_code=204)
def remove_rule(rule_id: int, conn: Conn):
    _get_rule(conn, rule_id)
    with conn:
        delete_rule(conn, rule_id)
    return Response(status_code=204)


@router.post("/test")
def preview(body: PreviewBody, conn: Conn):
    pattern = body.pattern.strip()
    try:
        _validate(pattern, body.match, body.direction)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    rule = _Rule(0, pattern, body.match, body.direction, None, 0)
    counts = Counter(label_norm for label_norm, amount_cents, account_id in conn.execute(
        "SELECT label_norm, amount_cents, account_id FROM transactions WHERE mirror_of IS NULL")
        if _matches(rule, label_norm, amount_cents, account_id))
    labels = sorted(counts, key=lambda label: (-counts[label], label))[:PREVIEW_LIMIT]
    return {"labels": labels, "total": len(counts), "tx_count": sum(counts.values())}


TAB = Tab("regles", "Règles", 70, router)
