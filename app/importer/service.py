"""Service d'import : dédoublonnage, réconciliation par section, conflits, identité stable, ré-analyse.

Toute l'écriture d'un import (ou d'une ré-analyse de document) se fait dans une seule transaction
SQLite. `on_new_transactions(conn, ids)` (une passe complète de classement) est appelé une seule fois
par opération et ne doit pas valider : à la fin de la transaction d'un import ou d'une acceptation de
conflit, et pour une ré-analyse dans une transaction à part, après la boucle des documents, avec les ids
de tous les documents.
"""
import hashlib
import json
import logging
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
from app.importer.amounts import from_cents, to_cents
from app.importer.base import StatementParser
from app.importer.validate import reconciliation_gap
from app.model import ParsedDocument, ParsedSection, ParsedTransaction, ParseError
from app.store import repo

log = logging.getLogger(__name__)

OnNewTransactions = Callable[[sqlite3.Connection, list[int]], None]

KEPT_OLD_VERSION = "ancienne version conservée"


@dataclass
class SectionReport:
    account_number: str
    statement_date: date
    status: str          # ok | error | conflict | superseded
    gap: Decimal
    tx_count: int        # transactions lues dans la section (insérées seulement si `ok`)
    info_lines: int = 0
    section_id: int | None = None
    message: str | None = None
    orphaned_overrides: int = 0     # reclassements manuels qui ne retrouvent plus leur opération


@dataclass
class ImportReport:
    document_id: int | None
    duplicate_file: bool
    sections: list[SectionReport]
    error: str | None


@dataclass
class PendingVersion:
    """Autre version d'un relevé dont la version retenue vient d'être supprimée : rien n'est importé
    automatiquement, elle repasse en `conflict` pour être tranchée dans l'onglet Relevés."""
    account_number: str
    statement_date: date
    section_id: int
    previous_status: str     # conflict | superseded


@dataclass
class DeleteReport:
    document_id: int
    deleted_transactions: int
    reclassified_mirrors: int       # jambes miroir d'autres relevés désappariées puis reclassées
    pending_versions: list[PendingVersion]


@dataclass(frozen=True)
class _KeyedTx:
    tx: ParsedTransaction
    label_norm: str
    key: str


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
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _parse(pdf_path: Path, parsers: list[StatementParser]) -> tuple[StatementParser, ParsedDocument]:
    for parser in parsers:
        if parser.can_parse(pdf_path):
            return parser, parser.parse(pdf_path)
    raise ParseError("format de relevé non reconnu")


def _report(section: ParsedSection, status: str, gap: Decimal, section_id: int | None,
            message: str | None = None, orphaned_overrides: int = 0) -> SectionReport:
    return SectionReport(section.account_number, section.statement_date, status, gap,
                         len(section.transactions), section.info_lines, section_id, message, orphaned_overrides)


def _keyed(section: ParsedSection) -> list[_KeyedTx]:
    """Identité stable de chaque transaction ; `rank` = rang parmi les doublons exacts de la section."""
    keyed = []
    ranks: Counter = Counter()
    for t in section.transactions:
        norm = normalize_label(t.label)
        identity = (t.op_date, to_cents(t.amount), norm)
        rank = ranks[identity]
        ranks[identity] += 1
        keyed.append(_KeyedTx(t, norm, tx_key(section.account_number, t.op_date, t.amount, norm, rank)))
    return keyed


def _slots(rows: list[tuple[str, str, int]]) -> list[tuple[tuple[str, int, int], str]]:
    """((op_date, montant, rang parmi les opérations de même date et même montant), tx_key) de chaque
    ligne (tx_key, op_date, amount_cents), dans l'ordre de la section."""
    ranks: Counter = Counter()
    slots = []
    for key, op_date, cents in rows:
        slots.append(((op_date, cents, ranks[(op_date, cents)]), key))
        ranks[(op_date, cents)] += 1
    return slots


def _carry_overrides(conn: sqlite3.Connection, old_section_id: int, keyed: list[_KeyedTx]) -> int:
    """Avant de remplacer les opérations de `old_section_id` par `keyed` : reporte les reclassements
    manuels dont l'identité change (libellé relu autrement) sur l'opération de même date, même montant
    et même rang ; renvoie le nombre de ceux qui ne trouvent aucune correspondance.

    Une identité cible déjà occupée (opération conservée telle quelle, ou autre reclassement manuel)
    n'est jamais écrasée : le reclassement reporté est alors compté comme orphelin."""
    old_rows = repo.section_transactions(conn, old_section_id)
    overrides = repo.manual_overrides_for(conn, [key for key, _, _ in old_rows])
    if not overrides:
        return 0
    new_keys = {k.key for k in keyed}
    new_by_slot = dict(_slots([(k.key, k.tx.op_date.isoformat(), to_cents(k.tx.amount)) for k in keyed]))
    kept = {key for key, _, _ in old_rows} & new_keys
    occupied = kept | set(repo.manual_overrides_for(conn, list(new_keys)))
    moves, orphaned = {}, 0
    for slot, old_key in _slots(old_rows):
        if old_key not in overrides or old_key in new_keys:     # identité inchangée : rien à faire
            continue
        target = new_by_slot.get(slot)
        if target is None or target in occupied:
            orphaned += 1
        else:
            moves[old_key] = target
            occupied.add(target)
    return orphaned + repo.move_manual_overrides(conn, moves)


def _collision_message(conn: sqlite3.Connection, keyed: list[_KeyedTx],
                       exclude_section_id: int | None = None) -> str | None:
    hit = repo.find_key_collision(conn, [k.key for k in keyed], exclude_section_id)
    if hit is None:
        return None
    number, statement_date = hit
    return f"opérations déjà importées avec le relevé du compte {number} au {statement_date or 'inconnu'}"


def _status_for(conn: sqlite3.Connection, account_id: int, statement_date: str, gap: Decimal,
                digest: str, exclude_id: int | None = None) -> str:
    if gap != 0:
        return "error"
    existing = repo.find_ok_section(conn, account_id, statement_date, exclude_id)
    if existing is None:
        return "ok"
    return "superseded" if existing[1] == digest else "conflict"


def _insert_transactions(conn: sqlite3.Connection, section_id: int, account_id: int,
                         keyed: list[_KeyedTx], pending: list[int], unmirrored: list[int] = ()) -> None:
    """Insère les transactions et ajoute à `pending` leurs ids et les anciennes jambes miroir `unmirrored`
    (leur source vient d'être supprimée : elles seront reclassées ou ré-appariées au lieu de garder une
    catégorie copiée périmée). Le classement est lancé une seule fois par opération, voir `_classify`."""
    pending += [repo.insert_transaction(conn, section_id, account_id, k.key, k.tx.op_date.isoformat(),
                                        k.tx.value_date.isoformat(), k.tx.label, k.label_norm,
                                        to_cents(k.tx.amount))
                for k in keyed]
    pending += unmirrored


def _classify(conn: sqlite3.Connection, on_new_transactions: OnNewTransactions, pending: list[int]) -> None:
    """Un seul appel du classement (une passe complète de la chaîne) par import, acceptation ou ré-analyse.

    Les ids supprimés depuis leur ajout (jambe miroir d'une section ré-analysée ensuite) sont écartés.
    """
    if pending:
        existing = {row[0] for row in conn.execute("SELECT id FROM transactions")}
        on_new_transactions(conn, [i for i in dict.fromkeys(pending) if i in existing])


def _import_section(conn: sqlite3.Connection, document_id: int, section: ParsedSection,
                    pending: list[int]) -> SectionReport:
    account_id = repo.upsert_account(conn, section.account_number, section.account_label)
    statement_date = section.statement_date.isoformat()
    gap, digest = reconciliation_gap(section), content_hash(section)
    status = _status_for(conn, account_id, statement_date, gap, digest)
    keyed = _keyed(section)
    message = _collision_message(conn, keyed) if status == "ok" else None
    if message is not None:
        status = "error"
    section_id = repo.insert_section(conn, document_id, account_id, statement_date,
                                     to_cents(section.opening), to_cents(section.closing), to_cents(gap),
                                     status, digest, message)
    if status == "ok":
        _insert_transactions(conn, section_id, account_id, keyed, pending)
    return _report(section, status, gap, section_id, message)


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
    try:
        stored.write_bytes(pdf_bytes)
        with _transaction(conn):
            imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            document_id = repo.insert_document(conn, sha, filename, stored.name, parser.name,
                                               doc.statement_date.isoformat(), imported_at)
            pending: list[int] = []
            reports = [_import_section(conn, document_id, s, pending) for s in doc.sections]
            _classify(conn, on_new_transactions, pending)
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    return ImportReport(document_id, False, reports, None)


def resolve_conflict(conn: sqlite3.Connection, section_id: int, accept: bool,
                     on_new_transactions: OnNewTransactions, *, pdf_dir: Path | None = None,
                     parsers: list[StatementParser] | None = None) -> int:
    """Tranche une section `conflict`. Accepter re-lit le PDF conservé : `pdf_dir` et `parsers` requis.

    Renvoie le nombre de reclassements manuels de l'ancienne version qui ne retrouvent pas leur opération.
    """
    row = repo.get_section(conn, section_id)
    if row is None or row[4] != "conflict":
        raise ValueError(f"la section {section_id} n'est pas en conflit")
    _, account_id, account_number, statement_date, _, stored_path = row
    if not accept:
        with _transaction(conn):
            repo.set_section_status(conn, section_id, "superseded")
        return 0
    if pdf_dir is None or parsers is None:
        raise ValueError("pdf_dir et parsers sont requis pour accepter la nouvelle version")

    _, doc = _parse(pdf_dir / stored_path, parsers)
    section = next((s for s in doc.sections if s.account_number == account_number
                    and s.statement_date.isoformat() == statement_date), None)
    if section is None:
        raise ParseError(f"section {account_number} au {statement_date} introuvable dans le PDF")
    gap, digest = reconciliation_gap(section), content_hash(section)
    opening, closing = to_cents(section.opening), to_cents(section.closing)
    with _transaction(conn):
        if gap != 0:     # le parseur a changé depuis : l'ancienne version reste en place
            repo.update_section(conn, section_id, opening, closing, to_cents(gap), "error", digest)
            return 0
        old = repo.find_ok_section(conn, account_id, statement_date, exclude_id=section_id)
        if old is not None and old[1] == digest:     # contenu désormais identique : rien à remplacer
            repo.update_section(conn, section_id, opening, closing, 0, "superseded", digest)
            return 0
        keyed = _keyed(section)
        message = _collision_message(conn, keyed, None if old is None else old[0])
        if message is not None:
            repo.update_section(conn, section_id, opening, closing, 0, "error", digest, message)
            return 0
        unmirrored: list[int] = []
        orphaned = 0
        if old is not None:
            orphaned = _carry_overrides(conn, old[0], keyed)
            repo.set_section_status(conn, old[0], "superseded")
            unmirrored = repo.delete_section_transactions(conn, old[0])
        repo.update_section(conn, section_id, opening, closing, 0, "ok", digest)
        pending: list[int] = []
        _insert_transactions(conn, section_id, account_id, keyed, pending, unmirrored)
        _classify(conn, on_new_transactions, pending)
    return orphaned


def delete_document(conn: sqlite3.Connection, document_id: int, pdf_dir: Path,
                    on_new_transactions: OnNewTransactions) -> DeleteReport | None:
    """Supprime un document importé, ses sections et leurs transactions ; None si le document n'existe pas.

    - les jambes miroir appariées à ces transactions dans d'autres relevés sont désappariées puis reclassées
      (une seule passe de classement) ;
    - si une section `ok` supprimée avait d'autres versions (`conflict` ou `superseded`) dans d'autres
      documents, aucune n'est importée automatiquement : elles passent en `conflict` (à trancher) et sont
      signalées dans le rapport ;
    - les reclassements manuels (`manual_overrides`) sont conservés : ils se ré-appliquent à la réimportation.

    Tout se fait dans une seule transaction ; le PDF conservé n'est effacé qu'après la validation.
    """
    stored_path = repo.get_document_path(conn, document_id)
    if stored_path is None:
        return None
    sections = conn.execute(
        "SELECT s.id, s.account_id, a.number, s.statement_date, s.status FROM sections s "
        "JOIN accounts a ON a.id = s.account_id WHERE s.document_id = ? ORDER BY s.id", (document_id,)).fetchall()
    with _transaction(conn):
        deleted = _count_document_transactions(conn, document_id)
        unmirrored: list[int] = []
        pending_versions: list[PendingVersion] = []
        for section_id, account_id, number, statement_date, status in sections:
            unmirrored += repo.delete_section_transactions(conn, section_id)
            if status != "ok":
                continue
            for other_id, other_status in repo.other_versions(conn, account_id, statement_date, document_id):
                if other_status in ("conflict", "superseded"):
                    repo.set_section_status(conn, other_id, "conflict")
                    pending_versions.append(PendingVersion(number, date.fromisoformat(statement_date), other_id,
                                                           other_status))
        repo.delete_document_rows(conn, document_id)
        existing = {row[0] for row in conn.execute("SELECT id FROM transactions")}
        reclassified = [i for i in dict.fromkeys(unmirrored) if i in existing]
        _classify(conn, on_new_transactions, reclassified)
    try:
        (pdf_dir / stored_path).unlink(missing_ok=True)
    except OSError as exc:      # base déjà validée : le fichier orphelin est sans conséquence
        log.warning("PDF conservé non supprimé (%s)", type(exc).__name__)
    return DeleteReport(document_id, deleted, len(reclassified), pending_versions)


def _count_document_transactions(conn: sqlite3.Connection, document_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE section_id IN "
                        "(SELECT id FROM sections WHERE document_id = ?)", (document_id,)).fetchone()[0]


def _reanalyse_section(conn: sqlite3.Connection, document_id: int, section: ParsedSection,
                       pending: list[int]) -> tuple[SectionReport, bool]:
    """Renvoie (rapport, ancienne version conservée malgré un échec)."""
    account_id = repo.upsert_account(conn, section.account_number, section.account_label)
    statement_date = section.statement_date.isoformat()
    existing = repo.find_document_section(conn, document_id, account_id, statement_date)
    if existing is None:     # section nouvellement reconnue par le parseur
        return _import_section(conn, document_id, section, pending), False
    section_id, old_status = existing
    gap, digest = reconciliation_gap(section), content_hash(section)
    keyed = _keyed(section)

    if old_status == "superseded":       # déjà tranché : on n'y touche pas
        return _report(section, "superseded", gap, section_id), False
    if old_status == "ok":
        message = KEPT_OLD_VERSION if gap != 0 else _collision_message(conn, keyed, section_id)
        if message is not None:
            if message != KEPT_OLD_VERSION:
                message = f"{message} ; {KEPT_OLD_VERSION}"
            return _report(section, "error", gap, section_id, message), True
        status = "ok"
        orphaned = _carry_overrides(conn, section_id, keyed)
        unmirrored = repo.delete_section_transactions(conn, section_id)
    else:                                # error ou conflict : réévaluée comme à l'import
        orphaned, unmirrored = 0, []
        status = _status_for(conn, account_id, statement_date, gap, digest, exclude_id=section_id)
        message = _collision_message(conn, keyed, section_id) if status == "ok" else None
        if message is not None:
            status = "error"
    repo.update_section(conn, section_id, to_cents(section.opening), to_cents(section.closing),
                        to_cents(gap), status, digest, message)
    if status == "ok":
        _insert_transactions(conn, section_id, account_id, keyed, pending, unmirrored)
    return _report(section, status, gap, section_id, message, orphaned), False


def _missing_ok_sections(conn: sqlite3.Connection, document_id: int, seen: set[int]) -> list[SectionReport]:
    """Sections `ok` du document que le parseur ne retrouve plus : conservées et signalées."""
    return [SectionReport(number, date.fromisoformat(statement_date), "error", from_cents(gap_cents), 0,
                          section_id=section_id, message=KEPT_OLD_VERSION)
            for section_id, number, statement_date, status, gap_cents
            in repo.list_document_sections(conn, document_id)
            if status == "ok" and section_id not in seen]


def reanalyse_all(conn: sqlite3.Connection, pdf_dir: Path, parsers: list[StatementParser],
                  on_new_transactions: OnNewTransactions) -> list[ImportReport]:
    reports = []
    pending: list[int] = []
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
        with _transaction(conn):
            repo.set_document_parser(conn, document_id, parser.name)
            results = [_reanalyse_section(conn, document_id, s, pending) for s in doc.sections]
            missing = _missing_ok_sections(conn, document_id, {r.section_id for r, _ in results})
        sections = [r for r, _ in results] + missing
        kept = [r.account_number for r, kept_old in results if kept_old] + [r.account_number for r in missing]
        error = f"section(s) {', '.join(kept)} : {KEPT_OLD_VERSION}" if kept else None
        reports.append(ImportReport(document_id, False, sections, error))
    if pending:                     # une seule passe de classement pour toute la ré-analyse
        try:
            with _transaction(conn):
                _classify(conn, on_new_transactions, pending)
        except Exception as exc:    # documents déjà validés : une nouvelle tentative, dans une transaction neuve
            log.warning("échec de la passe de classement après ré-analyse (%s), nouvelle tentative",
                        type(exc).__name__)
            with _transaction(conn):
                _classify(conn, on_new_transactions, pending)
    return reports
