"""Lignes de transaction renvoyées par les onglets et reclassement manuel (partagés par les onglets
Transactions et Catégories, pour que chacun reste utilisable seul)."""
import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel

from app.classify.rules import set_manual_category
from app.importer.amounts import from_cents

ROW_FROM = """
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN categories c ON c.id = t.category_id
"""
ROW_SELECT = """
    SELECT t.id, t.op_date, t.label, t.amount_cents, t.account_id, t.category_id, c.name, t.manual,
           t.mirror_of
""" + ROW_FROM


class ReclassBody(BaseModel):
    category_id: int | None


def row_to_dict(row: tuple) -> dict:
    tx_id, op_date, label, cents, account_id, category_id, category_name, manual, mirror_of = row
    return {"id": tx_id, "op_date": op_date, "label": label, "amount": from_cents(cents),
            "account_id": account_id, "category_id": category_id, "category_name": category_name,
            "manual": bool(manual), "mirror": mirror_of is not None}


def reclassify(conn: sqlite3.Connection, tx_id: int, category_id: int | None) -> dict:
    """Classement manuel (None : retour aux règles) ; renvoie la ligne mise à jour."""
    if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (tx_id,)).fetchone() is None:
        raise HTTPException(404, f"transaction {tx_id} introuvable")
    if category_id is not None and \
            conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone() is None:
        raise HTTPException(404, f"catégorie {category_id} introuvable")
    try:
        set_manual_category(conn, tx_id, category_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return row_to_dict(conn.execute(ROW_SELECT + " WHERE t.id = ?", (tx_id,)).fetchone())
