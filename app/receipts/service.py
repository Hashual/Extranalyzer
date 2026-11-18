"""Ingestion des tickets de caisse : dédoublonnage, stockage, rattachement des tickets CB, relecture.

Aucune fonction ne valide : l'appelant possède la transaction (comme app.importer.service).
"""
import hashlib
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.receipts.aisles import aisle_id
from app.receipts.base import ReceiptParser
from app.receipts.model import ParsedReceipt, ReceiptParseError

STAMP_RE = re.compile(r"(\d{8}-\d{6})")
SLIP_PREFIX = "Ticket de CB"
NOT_SUPPORTED = "enseigne pas encore prise en charge"


@dataclass
class FileResult:
    name: str
    outcome: str                    # stored | slip | duplicate | ignored | rejected
    receipt_id: int | None = None
    status: str | None = None
    message: str | None = None


def _euros(cents: int) -> str:
    return f"{abs(cents) // 100},{abs(cents) % 100:02d} €"


def _known(conn: sqlite3.Connection, sha: str) -> bool:
    return (conn.execute("SELECT 1 FROM receipts WHERE sha256=?", (sha,)).fetchone() is not None
            or conn.execute("SELECT 1 FROM receipt_slips WHERE sha256=?", (sha,)).fetchone() is not None)


def _tombstoned(conn: sqlite3.Connection, sha: str, message_id: str | None) -> bool:
    """Le fichier (ou son mail) appartient à un ticket supprimé : la relève ne le réimporte pas."""
    keys = [sha] + ([message_id] if message_id else [])
    marks = ",".join("?" * len(keys))
    return conn.execute(f"SELECT 1 FROM receipt_tombstones WHERE key IN ({marks})", keys).fetchone() is not None


def delete_receipt(conn: sqlite3.Connection, receipt_id: int) -> list[str] | None:
    """Supprime le ticket, ses lignes, paiements et tickets CB, et laisse des pierres tombales (empreinte du
    ticket, des tickets CB et identifiant du mail) pour que la relève ne le réimporte pas.

    Renvoie les fichiers PDF désormais orphelins (à effacer après validation), ou None si le ticket n'existe pas.
    """
    row = conn.execute("SELECT sha256, stored_path, message_id FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        return None
    sha, stored, message_id = row
    slips = conn.execute("SELECT sha256, stored_path FROM receipt_slips WHERE receipt_id=?", (receipt_id,)).fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    marks = [(sha, "sha256")] + [(s, "sha256") for s, _ in slips] + ([(message_id, "message_id")] if message_id else [])
    conn.executemany("INSERT OR IGNORE INTO receipt_tombstones(key, kind, origin_sha, deleted_at) VALUES (?, ?, ?, ?)",
                     [(key, kind, sha, now) for key, kind in marks])
    conn.execute("DELETE FROM receipt_lines WHERE receipt_id=?", (receipt_id,))
    conn.execute("DELETE FROM receipt_payments WHERE receipt_id=?", (receipt_id,))
    conn.execute("DELETE FROM receipt_slips WHERE receipt_id=?", (receipt_id,))
    conn.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    paths = [stored] + [path for _, path in slips]
    return [p for p in dict.fromkeys(paths)
            if not conn.execute("SELECT 1 FROM receipts WHERE stored_path=? UNION ALL "
                                "SELECT 1 FROM receipt_slips WHERE stored_path=?", (p, p)).fetchone()]


def _detect(data: bytes, filename: str, parsers: list[ReceiptParser]) -> tuple[ReceiptParser | None, str]:
    """Premier lecteur qui reconnaît le PDF, avec la nature du fichier ; sans lecteur, le nom décide."""
    if parsers:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.pdf"
            probe.write_bytes(data)
            for parser in parsers:
                kind = parser.kind(probe)
                if kind:
                    return parser, kind
    return None, "cb_slip" if filename.startswith(SLIP_PREFIX) else "receipt"


def write_parsed(conn: sqlite3.Connection, receipt_id: int, parsed: ParsedReceipt) -> None:
    """Remplace lignes et paiements ; les totaux doivent concorder (contrôles bloquants).

    Chaque ligne est rattachée au rayon de l'enseigne du ticket (créé sans famille s'il est inconnu).
    """
    for amounts in ([line.amount_cents for line in parsed.lines], [p.amount_cents for p in parsed.payments]):
        gap = sum(amounts) - parsed.total_cents
        if gap:
            raise ReceiptParseError(f"écart de {_euros(gap)}")
    store = conn.execute("SELECT store FROM receipts WHERE id=?", (receipt_id,)).fetchone()[0]
    conn.execute("DELETE FROM receipt_lines WHERE receipt_id=?", (receipt_id,))
    conn.execute("DELETE FROM receipt_payments WHERE receipt_id=?", (receipt_id,))
    lines = [(receipt_id, i, aisle_id(conn, store, line.aisle), line.label, line.quantity, line.unit_cents,
              line.amount_cents, line.kind) for i, line in enumerate(parsed.lines)]
    conn.executemany(
        "INSERT INTO receipt_lines(receipt_id, rank, aisle_id, label, quantity, unit_cents, amount_cents, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", lines)
    conn.executemany(
        "INSERT INTO receipt_payments(receipt_id, rank, method, amount_cents) VALUES (?, ?, ?, ?)",
        [(receipt_id, i, p.method, p.amount_cents) for i, p in enumerate(parsed.payments)])


def _read(conn: sqlite3.Connection, receipt_id: int, parser: ReceiptParser, pdf: Path) -> tuple[str, str | None]:
    """Lit le PDF et met à jour le ticket : (statut, message). Un échec met le ticket en `error`."""
    try:
        try:
            parsed = parser.parse(pdf)
        except ReceiptParseError:
            raise
        except Exception as exc:        # un lecteur ne doit jamais interrompre toute une relève
            raise ReceiptParseError(f"lecture impossible ({type(exc).__name__})") from exc
        write_parsed(conn, receipt_id, parsed)      # une erreur sqlite3 se propage : le message sera rejoué
    except ReceiptParseError as exc:
        conn.execute("UPDATE receipts SET status='error', message=? WHERE id=?", (str(exc), receipt_id))
        return "error", str(exc)
    conn.execute("UPDATE receipts SET status='ok', message=NULL, shop=?, purchased_at=?, total_cents=? "
                 "WHERE id=?", (parsed.shop, parsed.purchased_at.isoformat(timespec="seconds"),
                                parsed.total_cents, receipt_id))
    return "ok", None


def ingest_file(conn: sqlite3.Connection, data: bytes, filename: str, receipts_dir: Path,
                parsers: list[ReceiptParser], *, origin: str, store: str | None = None,
                message_id: str | None = None) -> FileResult:
    sha = hashlib.sha256(data).hexdigest()
    if _known(conn, sha):
        return FileResult(filename, "duplicate")
    if origin == "imap" and _tombstoned(conn, sha, message_id):
        return FileResult(filename, "ignored", message="ticket supprimé")
    parser, kind = _detect(data, filename, parsers)
    if parser is None and store is None:
        return FileResult(filename, "rejected", message=NOT_SUPPORTED)
    store = store or parser.store
    match = STAMP_RE.search(filename)
    stamp = match.group(1) if match else None

    if origin == "upload":      # dépôt explicite : l'utilisateur rajoute le ticket, la suppression est levée
        conn.execute("DELETE FROM receipt_tombstones WHERE key=? OR origin_sha=?", (sha, sha))

    receipts_dir.mkdir(parents=True, exist_ok=True)
    stored = receipts_dir / f"{sha}.pdf"
    stored.write_bytes(data)

    if kind == "cb_slip":
        receipt = None
        if stamp:
            receipt = conn.execute("SELECT id FROM receipts WHERE stamp=? ORDER BY id LIMIT 1",
                                   (stamp,)).fetchone()
        conn.execute("INSERT INTO receipt_slips(sha256, stored_path, stamp, receipt_id) VALUES (?, ?, ?, ?)",
                     (sha, stored.name, stamp, receipt[0] if receipt else None))
        return FileResult(filename, "slip", receipt[0] if receipt else None)

    if message_id and conn.execute("SELECT 1 FROM receipts WHERE message_id=?", (message_id,)).fetchone():
        message_id = None       # un mail avec plusieurs tickets : l'identifiant reste sur le premier
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO receipts(store, stamp, sha256, stored_path, origin, message_id, status, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending_parser', ?)",
        (store, stamp, sha, stored.name, origin, message_id, imported_at))
    receipt_id = cur.lastrowid
    status, message = "pending_parser", None
    if parser is not None:
        status, message = _read(conn, receipt_id, parser, stored)
    if stamp:
        conn.execute("UPDATE receipt_slips SET receipt_id=? WHERE stamp=? AND receipt_id IS NULL",
                     (receipt_id, stamp))
    return FileResult(filename, "stored", receipt_id, status, message)


def reread(conn: sqlite3.Connection, receipts_dir: Path, parsers: list[ReceiptParser],
           only_pending: bool = False) -> Counter:
    """Relit les tickets conservés (tous, ou seulement `pending_parser`) avec le lecteur de leur enseigne.

    Renvoie le nombre de tickets par statut obtenu ('ok', 'error') ; une enseigne sans lecteur est ignorée.
    """
    by_store = {p.store: p for p in parsers}
    sql = "SELECT id, store, stored_path FROM receipts"
    if only_pending:
        sql += " WHERE status='pending_parser'"
    counts: Counter = Counter()
    for receipt_id, store, stored_path in conn.execute(sql + " ORDER BY id").fetchall():
        parser = by_store.get(store)
        if parser is None:
            continue
        status, _ = _read(conn, receipt_id, parser, receipts_dir / stored_path)
        counts[status] += 1
    return counts
