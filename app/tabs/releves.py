"""Onglet « Relevés » : documents importés et leurs sections (statut de réconciliation, écart, opérations).

Les actions (trancher un conflit, ré-analyser) passent par les routes du cœur `/api/sections/{id}/resolve`
et `/api/reanalyse`. Aucun numéro de compte complet n'est exposé : seulement ses 4 derniers caractères.
"""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import get_conn
from app.importer.amounts import from_cents
from app.tabs.registry import Tab
from app.web_json import mask_numbers, to_jsonable

router = APIRouter()


def _gap_message(status: str, gap_cents: int) -> str | None:
    if status != "error":
        return None
    return f"écart de {from_cents(gap_cents):.2f} €".replace(".", ",")


@router.get("/list")
def list_documents(conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    sections: dict[int, list[dict]] = {}
    for sid, doc_id, label, number, statement_date, status, gap_cents, tx_count in conn.execute(
            """SELECT s.id, s.document_id, a.label, a.number, s.statement_date, s.status, s.gap_cents,
                      (SELECT COUNT(*) FROM transactions t WHERE t.section_id = s.id)
               FROM sections s JOIN accounts a ON a.id = s.account_id ORDER BY s.id"""):
        sections.setdefault(doc_id, []).append({
            "id": sid, "account_label": mask_numbers(label), "number_tail": number[-4:],
            "statement_date": statement_date, "status": status, "gap": from_cents(gap_cents),
            "tx_count": tx_count, "message": _gap_message(status, gap_cents)})
    documents = [{"id": doc_id, "filename": mask_numbers(filename), "statement_date": statement_date,
                  "imported_at": imported_at, "sections": sections.get(doc_id, [])}
                 for doc_id, filename, statement_date, imported_at in conn.execute(
                     "SELECT id, filename, statement_date, imported_at FROM documents "
                     "ORDER BY statement_date DESC, imported_at DESC, id DESC")]
    return to_jsonable({"documents": documents})


TAB = Tab("releves", "Relevés", 60, router)
