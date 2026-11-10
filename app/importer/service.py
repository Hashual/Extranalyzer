"""Service d'import : dédoublonnage, réconciliation par section, conflits, identité stable, ré-analyse.

Toute l'écriture d'un import (ou d'une ré-analyse de document) se fait dans une seule transaction
SQLite ; `on_new_transactions(conn, ids)` est appelé dans cette transaction et ne doit pas valider.
"""
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.classify.normalize import normalize_label
from app.importer.amounts import to_cents
from app.importer.base import StatementParser
from app.importer.validate import reconciliation_gap
from app.model import ParsedDocument, ParsedSection, ParseError
from app.store import repo

OnNewTransactions = Callable[[sqlite3.Connection, list[int]], None]


@dataclass
class SectionReport:
    account_number: str
    statement_date: date
    status: str          # ok | error | conflict | superseded
    gap: Decimal
    tx_count: int        # transactions lues dans la section (insérées seulement si `ok`)
    info_lines: int = 0
    section_id: int | None = None


@dataclass
class ImportReport:
    document_id: int | None
    duplicate_file: bool
    sections: list[SectionReport]
    error: str | None


class DuplicateTransactionError(Exception):
    """Une opération de la section existe déjà, rattachée à un autre relevé."""


def tx_key(account_number: str, op_date: date, amount: Decimal, label_norm: str, rank: int) -> str:
    raw = f"{account_number}|{op_date.isoformat()}|{to_cents(amount)}|{label_norm}|{rank}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def content_hash(section: ParsedSection) -> str:
    payload = [to_cents(section.opening), to_cents(section.closing),
               [[t.op_date.isoformat(), t.value_date.isoformat(), to_cents(t.amount), t.label]
                for t in section.transactions]]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


@contextmanager
def _transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _parse(pdf_path: Path, parsers: list[StatementParser]) -> tuple[StatementParser, ParsedDocument]:
    for parser in parsers:
        if parser.can_parse(pdf_path):
            return parser, parser.parse(pdf_path)
    raise ParseError("format de relevé non reconnu")


def _report(section: ParsedSection, status: str, gap: Decimal, section_id: int | None) -> SectionReport:
    return SectionReport(section.account_number, section.statement_date, status, gap,
                         len(section.transactions), section.info_lines, section_id)


def _status_for(conn: sqlite3.Connection, account_id: int, statement_date: str, gap: Decimal,
                digest: str, exclude_id: int | None = None) -> str:
    if gap != 0:
        return "error"
    existing = repo.find_ok_section(conn, account_id, statement_date, exclude_id)
    if existing is None:
        return "ok"
    return "superseded" if existing[1] == digest else "conflict"


def _insert_transactions(conn: sqlite3.Connection, section_id: int, account_id: int,
                         section: ParsedSection, on_new_transactions: OnNewTransactions) -> None:
    ids = []
    ranks: Counter = Counter()
    for t in section.transactions:
        norm = normalize_label(t.label)
        identity = (t.op_date, to_cents(t.amount), norm)
        rank = ranks[identity]
        ranks[identity] += 1
        key = tx_key(section.account_number, t.op_date, t.amount, norm, rank)
        if repo.transaction_exists(conn, key):
            raise DuplicateTransactionError(
                f"opération du {t.op_date.isoformat()} sur le compte {section.account_number} "
                f"déjà importée depuis un autre relevé")
        ids.append(repo.insert_transaction(conn, section_id, account_id, key, t.op_date.isoformat(),
                                           t.value_date.isoformat(), t.label, norm, to_cents(t.amount)))
    if ids:
        on_new_transactions(conn, ids)


def _import_section(conn: sqlite3.Connection, document_id: int, section: ParsedSection,
                    on_new_transactions: OnNewTransactions) -> SectionReport:
    account_id = repo.upsert_account(conn, section.account_number, section.account_label)
    statement_date = section.statement_date.isoformat()
    gap, digest = reconciliation_gap(section), content_hash(section)
    status = _status_for(conn, account_id, statement_date, gap, digest)
    section_id = repo.insert_section(conn, document_id, account_id, statement_date,
                                     to_cents(section.opening), to_cents(section.closing), to_cents(gap),
                                     status, digest)
    if status == "ok":
        _insert_transactions(conn, section_id, account_id, section, on_new_transactions)
    return _report(section, status, gap, section_id)


def import_pdf(conn: sqlite3.Connection, pdf_bytes: bytes, filename: str, pdf_dir: Path,
               parsers: list[StatementParser], on_new_transactions: OnNewTransactions) -> ImportReport:
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    if repo.document_id_by_sha(conn, sha) is not None:
        return ImportReport(None, True, [], None)

    with tempfile.TemporaryDirectory() as tmp:
        upload = Path(tmp) / "upload.pdf"
        upload.write_bytes(pdf_bytes)
        try:
            parser, doc = _parse(upload, parsers)
        except ParseError as exc:
            return ImportReport(None, False, [], str(exc))

    pdf_dir.mkdir(parents=True, exist_ok=True)
    stored = pdf_dir / f"{sha}.pdf"
    stored.write_bytes(pdf_bytes)
    try:
        with _transaction(conn):
            imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            document_id = repo.insert_document(conn, sha, filename, stored.name, parser.name,
                                               doc.statement_date.isoformat(), imported_at)
            reports = [_import_section(conn, document_id, s, on_new_transactions) for s in doc.sections]
    except DuplicateTransactionError as exc:
        stored.unlink(missing_ok=True)
        return ImportReport(None, False, [], str(exc))
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    return ImportReport(document_id, False, reports, None)


def resolve_conflict(conn: sqlite3.Connection, section_id: int, accept: bool,
                     on_new_transactions: OnNewTransactions, *, pdf_dir: Path | None = None,
                     parsers: list[StatementParser] | None = None) -> None:
    """Tranche une section `conflict`. Accepter re-lit le PDF conservé : `pdf_dir` et `parsers` requis."""
    row = repo.get_section(conn, section_id)
    if row is None or row[4] != "conflict":
        raise ValueError(f"la section {section_id} n'est pas en conflit")
    _, account_id, account_number, statement_date, _, stored_path = row
    if not accept:
        with _transaction(conn):
            repo.set_section_status(conn, section_id, "superseded")
        return
    if pdf_dir is None or parsers is None:
        raise ValueError("pdf_dir et parsers sont requis pour accepter la nouvelle version")

    _, doc = _parse(pdf_dir / stored_path, parsers)
    section = next((s for s in doc.sections if s.account_number == account_number
                    and s.statement_date.isoformat() == statement_date), None)
    if section is None:
        raise ParseError(f"section {account_number} au {statement_date} introuvable dans le PDF")
    gap, digest = reconciliation_gap(section), content_hash(section)
    with _transaction(conn):
        if gap != 0:     # le parseur a changé depuis : l'ancienne version reste en place
            repo.update_section(conn, section_id, to_cents(section.opening), to_cents(section.closing),
                                to_cents(gap), "error", digest)
            return
        old = repo.find_ok_section(conn, account_id, statement_date, exclude_id=section_id)
        if old is not None:
            repo.set_section_status(conn, old[0], "superseded")
            repo.delete_section_transactions(conn, old[0])
        repo.update_section(conn, section_id, to_cents(section.opening), to_cents(section.closing), 0,
                            "ok", digest)
        _insert_transactions(conn, section_id, account_id, section, on_new_transactions)


def _reanalyse_section(conn: sqlite3.Connection, document_id: int, section: ParsedSection,
                       on_new_transactions: OnNewTransactions) -> tuple[SectionReport, bool]:
    """Renvoie (rapport, ancienne version conservée malgré un échec)."""
    account_id = repo.upsert_account(conn, section.account_number, section.account_label)
    statement_date = section.statement_date.isoformat()
    existing = repo.find_document_section(conn, document_id, account_id, statement_date)
    if existing is None:     # section nouvellement reconnue par le parseur
        return _import_section(conn, document_id, section, on_new_transactions), False
    section_id, old_status = existing
    gap, digest = reconciliation_gap(section), content_hash(section)

    if old_status == "superseded":       # déjà tranché : on n'y touche pas
        return _report(section, "superseded", gap, section_id), False
    if old_status == "ok":
        if gap != 0:
            return _report(section, "error", gap, section_id), True
        status = "ok"
        repo.delete_section_transactions(conn, section_id)
    else:                                # error ou conflict : réévaluée comme à l'import
        status = _status_for(conn, account_id, statement_date, gap, digest, exclude_id=section_id)
    repo.update_section(conn, section_id, to_cents(section.opening), to_cents(section.closing),
                        to_cents(gap), status, digest)
    if status == "ok":
        _insert_transactions(conn, section_id, account_id, section, on_new_transactions)
    return _report(section, status, gap, section_id), False


def reanalyse_all(conn: sqlite3.Connection, pdf_dir: Path, parsers: list[StatementParser],
                  on_new_transactions: OnNewTransactions) -> list[ImportReport]:
    reports = []
    for document_id, stored_path in repo.list_documents(conn):
        path = pdf_dir / stored_path
        if not path.is_file():
            reports.append(ImportReport(document_id, False, [], f"fichier conservé introuvable : {stored_path}"))
            continue
        try:
            parser, doc = _parse(path, parsers)
        except ParseError as exc:
            reports.append(ImportReport(document_id, False, [], str(exc)))
            continue
        try:
            with _transaction(conn):
                repo.set_document_parser(conn, document_id, parser.name)
                results = [_reanalyse_section(conn, document_id, s, on_new_transactions)
                           for s in doc.sections]
        except DuplicateTransactionError as exc:
            reports.append(ImportReport(document_id, False, [], str(exc)))
            continue
        kept = [r.account_number for r, kept_old in results if kept_old]
        error = (f"section(s) {', '.join(kept)} non réconciliée(s) après ré-analyse : ancienne version conservée"
                 if kept else None)
        reports.append(ImportReport(document_id, False, [r for r, _ in results], error))
    return reports
