from datetime import datetime
from pathlib import Path

import pytest

from app.receipts.model import ParsedReceipt, Payment, ReceiptLine, ReceiptParseError
from app.receipts.service import ingest_file, reread, write_parsed
from app.store.db import connect

SLIP = "Ticket de CB_01012025-101010.pdf"
RECEIPT = "Ticket 01012025-101010.pdf"


def pdf(tag: str) -> bytes:
    return b"%PDF-1.4\n" + tag.encode()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


class FakeParser:
    store = "superu"

    def __init__(self, total=1000, fail=False):
        self.total, self.fail = total, fail

    def kind(self, path: Path):
        return "receipt"

    def parse(self, path: Path) -> ParsedReceipt:
        if self.fail:
            raise ReceiptParseError("format illisible")
        return ParsedReceipt(
            "Magasin fictif", datetime(2025, 1, 1, 10, 10, 10), self.total,
            [ReceiptLine("Fruits", "Pommes", 2, 300, 600, "item"),
             ReceiptLine("Fruits", "Poires", 1, 500, 500, "item"),
             ReceiptLine("Remise", "Remise fidélité", 1, None, -100, "discount")],
            [Payment("card", self.total)])


def test_upload_without_parser_or_store_is_rejected(conn, tmp_path):
    r = ingest_file(conn, pdf("a"), "x.pdf", tmp_path / "r", [], origin="upload")
    assert r.outcome == "rejected" and "pas encore prise en charge" in r.message
    assert not list((tmp_path / "r").glob("*.pdf"))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_store_without_parser_is_pending(conn, tmp_path):
    r = ingest_file(conn, pdf("a"), RECEIPT, tmp_path, [], origin="imap", store="superu", message_id="<m1>")
    assert (r.outcome, r.status) == ("stored", "pending_parser")
    row = conn.execute("SELECT store, stamp, status, shop, purchased_at, total_cents, message_id, origin "
                       "FROM receipts WHERE id=?", (r.receipt_id,)).fetchone()
    assert row == ("superu", "01012025-101010", "pending_parser", None, None, None, "<m1>", "imap")
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_duplicate_file_is_skipped(conn, tmp_path):
    ingest_file(conn, pdf("a"), RECEIPT, tmp_path, [], origin="imap", store="superu")
    again = ingest_file(conn, pdf("a"), "autre nom.pdf", tmp_path, [], origin="upload", store="superu")
    assert again.outcome == "duplicate"
    ingest_file(conn, pdf("s"), SLIP, tmp_path, [], origin="imap", store="superu")
    assert ingest_file(conn, pdf("s"), SLIP, tmp_path, [], origin="imap", store="superu").outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_slips").fetchone()[0] == 1


def test_slip_waits_then_attaches_to_later_receipt(conn, tmp_path):
    s = ingest_file(conn, pdf("s"), SLIP, tmp_path, [], origin="imap", store="superu")
    assert s.outcome == "slip"
    assert conn.execute("SELECT receipt_id FROM receipt_slips").fetchone()[0] is None
    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [], origin="imap", store="superu")
    assert conn.execute("SELECT receipt_id FROM receipt_slips").fetchone()[0] == r.receipt_id


def test_slip_attaches_to_existing_receipt(conn, tmp_path):
    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [], origin="imap", store="superu")
    ingest_file(conn, pdf("s"), SLIP, tmp_path, [], origin="imap", store="superu")
    assert conn.execute("SELECT receipt_id FROM receipt_slips").fetchone()[0] == r.receipt_id


def test_slip_with_other_stamp_stays_pending(conn, tmp_path):
    ingest_file(conn, pdf("r"), "Ticket 02022025-111111.pdf", tmp_path, [], origin="imap", store="superu")
    ingest_file(conn, pdf("s"), SLIP, tmp_path, [], origin="imap", store="superu")
    assert conn.execute("SELECT receipt_id FROM receipt_slips").fetchone()[0] is None


def test_parser_fills_receipt(conn, tmp_path):
    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [FakeParser()], origin="upload")
    assert (r.outcome, r.status) == ("stored", "ok")
    row = conn.execute("SELECT store, shop, purchased_at, total_cents FROM receipts").fetchone()
    assert row == ("superu", "Magasin fictif", "2025-01-01T10:10:10", 1000)
    assert conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0] == 3
    assert conn.execute("SELECT a.store, a.name, a.family_id FROM receipt_lines l JOIN aisles a "
                        "ON a.id = l.aisle_id WHERE l.rank=0").fetchone() == ("superu", "FRUITS", None)
    assert conn.execute("SELECT method, amount_cents FROM receipt_payments").fetchall() == [("card", 1000)]


def test_parser_failure_is_recorded_as_error(conn, tmp_path):
    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [FakeParser(fail=True)], origin="upload")
    assert (r.outcome, r.status) == ("stored", "error") and "illisible" in r.message
    assert conn.execute("SELECT status FROM receipts").fetchone()[0] == "error"


def test_write_parsed_blocks_on_gap_and_keeps_old_lines(conn, tmp_path):
    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [FakeParser()], origin="upload")
    bad = FakeParser(total=1050).parse(Path("x"))
    bad.payments = [Payment("card", 1000)]
    with pytest.raises(ReceiptParseError, match="écart de 0,50 €"):
        write_parsed(conn, r.receipt_id, bad)
    bad = FakeParser().parse(Path("x"))
    bad.payments = [Payment("card", 900)]
    with pytest.raises(ReceiptParseError, match="écart de 1,00 €"):
        write_parsed(conn, r.receipt_id, bad)
    assert conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0] == 3


def test_reread_only_pending_picks_up_new_parser(conn, tmp_path):
    ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [], origin="imap", store="superu")
    ingest_file(conn, pdf("o"), "Ticket 02022025-111111.pdf", tmp_path, [], origin="imap", store="intermarche")
    counts = reread(conn, tmp_path, [FakeParser()], only_pending=True)
    assert counts["ok"] == 1
    rows = dict(conn.execute("SELECT store, status FROM receipts").fetchall())
    assert rows == {"superu": "ok", "intermarche": "pending_parser"}


def test_reread_all_replaces_lines(conn, tmp_path):
    ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [FakeParser()], origin="upload")
    counts = reread(conn, tmp_path, [FakeParser(total=1000)])
    assert counts["ok"] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0] == 3
    counts = reread(conn, tmp_path, [FakeParser(fail=True)])
    assert counts["error"] == 1
    assert conn.execute("SELECT status FROM receipts").fetchone()[0] == "error"


def test_unexpected_parser_exception_yields_error_status(conn, tmp_path):
    class Crashing(FakeParser):
        def parse(self, path):
            raise KeyError("x")

    r = ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [Crashing()], origin="upload")
    assert (r.outcome, r.status) == ("stored", "error") and "KeyError" in r.message


def test_database_error_during_write_propagates(conn, tmp_path, monkeypatch):
    import sqlite3

    import app.receipts.service as service

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(service, "write_parsed", boom)
    with pytest.raises(sqlite3.OperationalError):
        ingest_file(conn, pdf("r"), RECEIPT, tmp_path, [FakeParser()], origin="upload")
