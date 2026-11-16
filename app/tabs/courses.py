"""Onglet « Courses » : consommation par famille de rayons d'après les tickets de caisse, tickets et
leur rapprochement avec les débits, rayons à ranger, relève et dépôt manuel.

Toute la consommation compte : les articles d'un ticket lu (`status='ok'`) dont la date est dans la
période comptent en entier, quel que soit le moyen de paiement (titres-restaurant compris) et qu'il soit
relié ou non. Les tickets ne dépendent pas du sélecteur de comptes ; les débits Courses sans ticket
(« non détaillé ») et les débits reliés hors Courses le suivent, comme les autres onglets.

Les écritures passent par `app.state.write` (le verrou d'écriture de l'application), chacune dans sa
transaction.
"""
import logging
import re
import sqlite3
from dataclasses import asdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.analytics.periods import Period
from app.analytics.totals import SIGNED_AMOUNT, scope_clause
from app.deps import get_conn
from app.importer.amounts import from_cents
from app.receipts import PARSERS_RECEIPTS
from app.receipts.mailruntime import MailNotConfigured
from app.receipts.matching import candidates, link_manual, match_receipts
from app.receipts.service import FileResult, ingest_file, reread
from app.store.db import transactional
from app.tabs._params import Scope, scope
from app.tabs.registry import Tab
from app.web_json import to_jsonable

log = logging.getLogger(__name__)

COURSES = "Courses"             # catégorie racine des courses
UNSORTED = "À ranger"           # pseudo-famille (id 0) des rayons sans famille
PAYMENT_METHODS = ("card", "meal_voucher", "voucher", "cash", "other")
EVERYTHING = Period(date.min, date.max)     # pour ne garder que le filtre de comptes de scope_clause

# État du lien d'un ticket, sur `receipts r`. Un délié manuel (mode 'manual' sans débit) reste à relier :
# le rapprochement automatique ne le refait jamais.
CARD_CENTS = ("COALESCE((SELECT SUM(p.amount_cents) FROM receipt_payments p "
              "WHERE p.receipt_id = r.id AND p.method = 'card'), 0)")
LINK = f"""CASE WHEN r.status != 'ok' THEN NULL
    WHEN r.link_broken = 1 THEN 'broken'
    WHEN r.tx_key IS NOT NULL THEN r.link_mode
    WHEN {CARD_CENTS} <= 0 THEN 'no_card'
    ELSE 'to_link' END"""
COUNTED = "r.status = 'ok' AND date(r.purchased_at) BETWEEN ? AND ?"     # tickets comptés sur une période
RECEIPT_SELECT = f"""SELECT r.id, r.store, r.shop, r.purchased_at, r.total_cents, {CARD_CENTS}, r.status,
    {LINK} AS link, r.message, r.tx_key FROM receipts r"""

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class LinkBody(BaseModel):
    tx_key: str | None


class AisleBody(BaseModel):
    family_id: int | None


class FamilyBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class NotFound(LookupError):
    pass


def _bounds(p: Period) -> list[str]:
    return [p.start.isoformat(), p.end.isoformat()]


def _courses_root(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM categories WHERE parent_id IS NULL AND name = ?", (COURSES,)).fetchone()
    return row[0] if row else -1        # -1 : aucune catégorie (sous-arbre vide)


def _cents_or_none(cents: int | None) -> Decimal | None:
    return None if cents is None else from_cents(cents)


# --- Synthèse ----------------------------------------------------------------------------------

def _family_cents(conn: sqlite3.Connection, p: Period) -> dict[int, int]:
    """{famille (0 : à ranger): centimes} des lignes des tickets comptés."""
    return dict(conn.execute(
        f"SELECT COALESCE(a.family_id, 0), SUM(l.amount_cents) FROM receipt_lines l "
        f"JOIN receipts r ON r.id = l.receipt_id LEFT JOIN aisles a ON a.id = l.aisle_id "
        f"WHERE {COUNTED} GROUP BY 1", _bounds(p)).fetchall())


def _undetailed_cents(conn: sqlite3.Connection, s: Scope, p: Period, root: int) -> int:
    """Débits nets du sous-arbre Courses sur le périmètre, sans ticket lu relié (positif = dépense)."""
    if s.account_ids is not None and not s.account_ids:
        return 0
    where, params = scope_clause(p, s.account_ids)
    return conn.execute(
        f"SELECT COALESCE(SUM({SIGNED_AMOUNT['expense']}), 0) FROM transactions t "
        f"JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id "
        f"WHERE {where} AND (t.category_id = ? OR c.parent_id = ?) AND NOT EXISTS "
        f"(SELECT 1 FROM receipts r WHERE r.tx_key = t.tx_key AND r.link_broken = 0 AND r.status = 'ok')",
        params + [root, root]).fetchone()[0]


def _payment_cents(conn: sqlite3.Connection, p: Period) -> dict[str, int]:
    return dict(conn.execute(
        f"SELECT p.method, SUM(p.amount_cents) FROM receipt_payments p JOIN receipts r ON r.id = p.receipt_id "
        f"WHERE {COUNTED} GROUP BY 1", _bounds(p)).fetchall())


def _out_of_courses(conn: sqlite3.Connection, s: Scope, root: int) -> list[dict]:
    """Tickets comptés dont le débit relié (dans les comptes du sélecteur) est classé hors Courses."""
    if s.account_ids is not None and not s.account_ids:
        return []
    accounts, params = scope_clause(EVERYTHING, s.account_ids)
    rows = conn.execute(
        f"SELECT r.id, t.label, c.name FROM receipts r JOIN transactions t ON t.tx_key = r.tx_key "
        f"JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id "
        f"WHERE {COUNTED} AND r.link_broken = 0 AND {accounts} "
        f"AND (t.category_id IS NULL OR (t.category_id != ? AND COALESCE(c.parent_id, -1) != ?)) "
        f"ORDER BY r.purchased_at, r.id", _bounds(s.period) + params + [root, root]).fetchall()
    return [{"receipt_id": rid, "tx_label": label, "category_name": name} for rid, label, name in rows]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    links = dict(conn.execute(f"SELECT {LINK} AS link, COUNT(*) FROM receipts r GROUP BY link").fetchall())
    statuses = dict(conn.execute("SELECT status, COUNT(*) FROM receipts GROUP BY status").fetchall())
    unsorted = conn.execute("SELECT COUNT(*) FROM aisles a WHERE a.family_id IS NULL AND EXISTS "
                            "(SELECT 1 FROM receipt_lines l WHERE l.aisle_id = a.id)").fetchone()[0]
    return {"to_link": links.get("to_link", 0), "broken": links.get("broken", 0), "unsorted_aisles": unsorted,
            "errors": statuses.get("error", 0), "pending_parser": statuses.get("pending_parser", 0)}


@router.get("/summary")
def summary(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    root = _courses_root(conn)
    current = _family_cents(conn, s.period)
    previous = _family_cents(conn, s.compare) if s.compare else {}
    undetailed = _undetailed_cents(conn, s, s.period, root)
    names = {0: (UNSORTED, float("inf"))} | {
        fid: (name, position) for fid, name, position in conn.execute("SELECT id, name, position FROM aisle_families")}
    families = [{"id": fid, "name": names[fid][0], "amount": from_cents(current.get(fid, 0)),
                 "compare": from_cents(previous.get(fid, 0)) if s.compare else None}
                for fid in sorted(current.keys() | previous.keys(),
                                  key=lambda f: (-current.get(f, 0), names[f][1], names[f][0]))]
    payments = _payment_cents(conn, s.period)
    payments["card"] = payments.get("card", 0) + undetailed
    return to_jsonable({
        "total": from_cents(sum(current.values()) + undetailed),
        "total_compare": from_cents(sum(previous.values()) + _undetailed_cents(conn, s, s.compare, root))
        if s.compare else None,
        "by_payment": {m: from_cents(payments.get(m, 0)) for m in PAYMENT_METHODS},
        "families": families,
        "undetailed": from_cents(undetailed),
        "undetailed_compare": from_cents(_undetailed_cents(conn, s, s.compare, root)) if s.compare else None,
        "counts": _counts(conn),
        "out_of_courses": _out_of_courses(conn, s, root),
    })


# --- Détail d'une famille ----------------------------------------------------------------------

def _avg_unit(cents: int, quantity: float) -> Decimal | None:
    if not quantity:
        return None
    return from_cents(int((Decimal(cents) / Decimal(str(quantity))).quantize(Decimal(1), ROUND_HALF_UP)))


@router.get("/family/{family_id}")
def family(family_id: int, conn: Conn, s: Annotated[Scope, Depends(scope)]):
    """Rayons d'une famille (0 : à ranger) et, pour chacun, ses produits sur la période."""
    if family_id == 0:
        cond, cond_params = "a.family_id IS NULL", []
    elif conn.execute("SELECT 1 FROM aisle_families WHERE id = ?", (family_id,)).fetchone() is None:
        raise HTTPException(404, f"famille {family_id} introuvable")
    else:
        cond, cond_params = "a.family_id = ?", [family_id]
    base = f"FROM receipt_lines l JOIN receipts r ON r.id = l.receipt_id JOIN aisles a ON a.id = l.aisle_id " \
           f"WHERE {COUNTED} AND {cond}"

    def per_aisle(p: Period) -> dict[int, tuple[str, str, int]]:
        return {aid: (name, store, cents) for aid, name, store, cents in conn.execute(
            f"SELECT a.id, a.name, a.store, SUM(l.amount_cents) {base} GROUP BY a.id",
            _bounds(p) + cond_params)}

    current = per_aisle(s.period)
    previous = per_aisle(s.compare) if s.compare else {}
    products: dict[int, list[dict]] = {}
    for aid, label, quantity, cents, purchases in conn.execute(
            f"SELECT l.aisle_id, l.label, SUM(l.quantity), SUM(l.amount_cents), COUNT(DISTINCT l.receipt_id) "
            f"{base} GROUP BY l.aisle_id, l.label ORDER BY SUM(l.amount_cents) DESC, l.label",
            _bounds(s.period) + cond_params):
        products.setdefault(aid, []).append({"label": label, "quantity": round(quantity, 3),
                                             "amount": from_cents(cents), "avg_unit": _avg_unit(cents, quantity),
                                             "purchases": purchases})
    aisles = []
    for aid in current.keys() | previous.keys():
        name, store, _ = current.get(aid) or previous[aid]
        aisles.append({"id": aid, "name": name, "store": store,
                       "amount": from_cents(current[aid][2] if aid in current else 0),
                       "compare": from_cents(previous[aid][2] if aid in previous else 0) if s.compare else None,
                       "products": products.get(aid, [])})
    aisles.sort(key=lambda a: (-a["amount"], a["name"]))
    return to_jsonable({"aisles": aisles})


# --- Tickets -----------------------------------------------------------------------------------

def _receipt_row(row: tuple) -> dict:
    rid, store, shop, purchased_at, total, card, status, link, message, _ = row
    return {"id": rid, "store": store, "shop": shop, "purchased_at": purchased_at, "total": _cents_or_none(total),
            "card": from_cents(card), "status": status, "link": link, "message": message}


@router.get("/receipts")
def receipts(conn: Conn, s: Annotated[Scope, Depends(scope)]):
    """Tickets de la période, plus ceux qui demandent une action quelle que soit leur date (à relier, lien
    cassé, en erreur, en attente d'un lecteur)."""
    rows = conn.execute(
        f"SELECT * FROM ({RECEIPT_SELECT}) WHERE (status = 'ok' AND date(purchased_at) BETWEEN ? AND ?) "
        f"OR status != 'ok' OR link IN ('to_link', 'broken') "
        f"ORDER BY COALESCE(purchased_at, '9999') DESC, id DESC", _bounds(s.period)).fetchall()
    return to_jsonable([_receipt_row(r) for r in rows])


def _detail(conn: sqlite3.Connection, receipt_id: int) -> dict:
    row = conn.execute(f"{RECEIPT_SELECT} WHERE r.id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise NotFound(f"ticket {receipt_id} introuvable")
    data = _receipt_row(row)
    groups: dict[tuple, dict] = {}
    for aid, aisle, family_name, label, quantity, unit, cents, kind in conn.execute(
            "SELECT l.aisle_id, a.name, f.name, l.label, l.quantity, l.unit_cents, l.amount_cents, l.kind "
            "FROM receipt_lines l LEFT JOIN aisles a ON a.id = l.aisle_id "
            "LEFT JOIN aisle_families f ON f.id = a.family_id WHERE l.receipt_id = ? ORDER BY l.rank",
            (receipt_id,)):
        group = groups.setdefault(aid, {"aisle": aisle, "family": family_name, "lines": []})
        group["lines"].append({"label": label, "quantity": quantity, "unit": _cents_or_none(unit),
                               "amount": from_cents(cents), "kind": kind})
    data["lines"] = list(groups.values())
    data["payments"] = [{"method": method, "amount": from_cents(cents)} for method, cents in conn.execute(
        "SELECT method, amount_cents FROM receipt_payments WHERE receipt_id = ? ORDER BY rank", (receipt_id,))]
    tx = conn.execute("SELECT tx_key, op_date, label, amount_cents FROM transactions WHERE tx_key = ?",
                      (row[-1],)).fetchone() if row[-1] else None
    data["tx"] = None if tx is None else {"tx_key": tx[0], "op_date": tx[1], "label": tx[2],
                                          "amount": from_cents(tx[3])}
    data["candidates"] = candidates(conn, receipt_id) if data["status"] == "ok" else []
    return data


@router.get("/receipts/{receipt_id}")
def receipt(receipt_id: int, conn: Conn):
    try:
        return to_jsonable(_detail(conn, receipt_id))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/receipts/{receipt_id}/pdf")
def receipt_pdf(receipt_id: int, conn: Conn, request: Request):
    row = conn.execute("SELECT stored_path FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    path = None if row is None else request.app.state.receipts_dir / row[0]
    if path is None or not path.is_file():
        raise HTTPException(404, f"ticket {receipt_id} introuvable")
    return FileResponse(path, media_type="application/pdf")


@router.post("/receipts/{receipt_id}/link")
def link(receipt_id: int, body: LinkBody, request: Request):
    def run(conn: sqlite3.Connection) -> dict:
        if conn.execute("SELECT 1 FROM receipts WHERE id = ?", (receipt_id,)).fetchone() is None:
            raise NotFound(f"ticket {receipt_id} introuvable")
        if body.tx_key is not None and \
                conn.execute("SELECT 1 FROM transactions WHERE tx_key = ?", (body.tx_key,)).fetchone() is None:
            raise NotFound("débit introuvable")
        link_manual(conn, receipt_id, body.tx_key)
        return _detail(conn, receipt_id)
    try:
        return to_jsonable(request.app.state.write(transactional(run)))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


# --- Rayons et familles ------------------------------------------------------------------------

def _family_row(conn: sqlite3.Connection, family_id: int) -> dict:
    fid, name, position = conn.execute("SELECT id, name, position FROM aisle_families WHERE id = ?",
                                       (family_id,)).fetchone()
    return {"id": fid, "name": name, "position": position}


def _aisle_row(row: tuple) -> dict:
    aid, store, name, family_id, lines = row
    return {"id": aid, "store": store, "name": name, "family_id": family_id, "lines": lines}


AISLE_SELECT = ("SELECT a.id, a.store, a.name, a.family_id, "
                "(SELECT COUNT(*) FROM receipt_lines l WHERE l.aisle_id = a.id) FROM aisles a")


@router.get("/aisles")
def aisles(conn: Conn):
    families = [{"id": fid, "name": name, "position": position} for fid, name, position in
                conn.execute("SELECT id, name, position FROM aisle_families ORDER BY position, id")]
    rows = conn.execute(f"{AISLE_SELECT} ORDER BY a.family_id IS NOT NULL, a.store, a.name").fetchall()
    return {"families": families, "aisles": [_aisle_row(r) for r in rows]}


@router.post("/aisles/{aisle_id}")
def set_aisle_family(aisle_id: int, body: AisleBody, request: Request):
    def run(conn: sqlite3.Connection) -> dict:
        if conn.execute("SELECT 1 FROM aisles WHERE id = ?", (aisle_id,)).fetchone() is None:
            raise NotFound(f"rayon {aisle_id} introuvable")
        if body.family_id is not None and \
                conn.execute("SELECT 1 FROM aisle_families WHERE id = ?", (body.family_id,)).fetchone() is None:
            raise NotFound(f"famille {body.family_id} introuvable")
        conn.execute("UPDATE aisles SET family_id = ? WHERE id = ?", (body.family_id, aisle_id))
        return _aisle_row(conn.execute(f"{AISLE_SELECT} WHERE a.id = ?", (aisle_id,)).fetchone())
    try:
        return request.app.state.write(transactional(run))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None


def _family_name(body: FamilyBody) -> str:
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(422, "nom de famille vide")
    return name


def _name_taken(conn: sqlite3.Connection, name: str, family_id: int | None = None) -> None:
    if conn.execute("SELECT 1 FROM aisle_families WHERE name = ? AND id IS NOT ?", (name, family_id)).fetchone():
        raise FileExistsError(f"la famille « {name} » existe déjà")


@router.post("/families")
def create_family(body: FamilyBody, request: Request):
    name = _family_name(body)

    def run(conn: sqlite3.Connection) -> dict:
        _name_taken(conn, name)
        position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM aisle_families").fetchone()[0]
        cur = conn.execute("INSERT INTO aisle_families(name, position) VALUES (?, ?)", (name, position))
        return _family_row(conn, cur.lastrowid)
    try:
        return request.app.state.write(transactional(run))
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/families/{family_id}")
def rename_family(family_id: int, body: FamilyBody, request: Request):
    name = _family_name(body)

    def run(conn: sqlite3.Connection) -> dict:
        if conn.execute("SELECT 1 FROM aisle_families WHERE id = ?", (family_id,)).fetchone() is None:
            raise NotFound(f"famille {family_id} introuvable")
        _name_taken(conn, name, family_id)
        conn.execute("UPDATE aisle_families SET name = ? WHERE id = ?", (name, family_id))
        return _family_row(conn, family_id)
    try:
        return request.app.state.write(transactional(run))
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from None


# --- Dépôt, relève, relecture ------------------------------------------------------------------

def _safe_name(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1].strip()
    return base[:255] or "ticket.pdf"


@router.post("/upload")
async def upload(request: Request, files: Annotated[list[UploadFile], File()]):
    """Chaque fichier est ingéré dans sa propre transaction, puis les tickets sont rapprochés."""
    state = request.app.state
    max_bytes = state.max_upload_bytes

    def ingest(data: bytes, name: str) -> FileResult:
        try:
            return state.write(transactional(lambda conn: ingest_file(
                conn, data, name, state.receipts_dir, PARSERS_RECEIPTS, origin="upload")))
        except Exception as exc:     # fichier hostile ou inattendu : rejeté, le serveur continue
            log.error("échec de l'ingestion d'un ticket (%s)", type(exc).__name__)
            return FileResult(name, "rejected", message="échec de l'analyse du ticket")

    results = []
    for file in files:
        name = _safe_name(file.filename)
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            results.append(FileResult(name, "rejected", message="fichier trop volumineux"))
        elif b"%PDF-" not in data[:1024]:
            results.append(FileResult(name, "rejected", message="le fichier n'est pas un PDF"))
        else:
            results.append(await run_in_threadpool(ingest, data, name))
    if any(r.outcome in ("stored", "slip") for r in results):
        await run_in_threadpool(state.write, transactional(match_receipts))
    return [asdict(r) for r in results]


@router.get("/sync")
def sync_status(conn: Conn, request: Request):
    labels = [{"label": label, "store": store, "last_success_at": success, "last_attempt_at": attempt,
               "last_error": error} for label, store, success, attempt, error in conn.execute(
        "SELECT label, store, last_success_at, last_attempt_at, last_error FROM mail_sync ORDER BY label")]
    return {"enabled": request.app.state.mail.enabled, "labels": labels}


@router.post("/sync")
async def sync_now(request: Request):
    """Un passage de relève (jamais en même temps qu'un passage de fond), suivi du rapprochement."""
    try:
        return await run_in_threadpool(request.app.state.mail.run_once)
    except MailNotConfigured as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/reread")
def reread_all(request: Request):
    state = request.app.state

    def run(conn: sqlite3.Connection) -> dict:
        counts = reread(conn, state.receipts_dir, PARSERS_RECEIPTS)
        match_receipts(conn)
        return dict(counts)
    return state.write(transactional(run))


TAB = Tab("courses", "Courses", 25, router)
