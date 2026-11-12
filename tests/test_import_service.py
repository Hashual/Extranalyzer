import hashlib
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from reportlab.pdfgen import canvas

from app.classify.normalize import normalize_label
from app.importer.creditmutuel import CreditMutuelParser
from app.importer.service import import_pdf, reanalyse_all, resolve_conflict, tx_key
from app.store.db import connect
from pdfgen import FSection, FTx, make_statement

CC = "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"
LIVRET = "LIVRET BLEU TEST N° 00000000002 en euros"
OPEN = ("CREDITEUR", "11/03/2024", "100,00")


def _cc_section(closing="1.087,50", vir="1.000,00"):
    return FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", closing), [
        FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 VILLEA", "MAGASIN ALPHA CARTE 0000"], debit="2,50"),
        FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit=vir),
        FTx("13/03/2024", "13/03/2024", ["PRLV SEPA BETA"], debit="10,00")])


def _livret_section():
    return FSection(LIVRET, ("CREDITEUR", "11/03/2024", "500,00"), ("CREDITEUR", "10/04/2024", "520,00"),
                    [FTx("15/03/2024", "15/03/2024", ["VIR EPARGNE TEST"], credit="20,00")])


def _pdf(tmp_path, name, sections, **kwargs):
    path = make_statement(tmp_path / name, "10 avril 2024", sections, **kwargs)
    return path.read_bytes()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, conn, ids):
        assert conn.in_transaction       # appelé dans la transaction de l'import
        self.calls.append(list(ids))


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def pdf_dir(tmp_path):
    return tmp_path / "pdfs"


PARSERS = [CreditMutuelParser()]


def _count(conn, sql="SELECT COUNT(*) FROM transactions", *args):
    return conn.execute(sql, args).fetchone()[0]


def _statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM sections ORDER BY id")]


def test_tx_key_is_sha1_of_identity():
    expected = hashlib.sha1("00000000001|2024-03-11|-250|PAIEMENT CB|0".encode()).hexdigest()
    assert tx_key("00000000001", date(2024, 3, 11), Decimal("-2.50"), "PAIEMENT CB", 0) == expected


def test_import_ok(tmp_path, conn, pdf_dir):
    data = _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()])
    rec = Recorder()
    report = import_pdf(conn, data, "a.pdf", pdf_dir, PARSERS, rec)

    assert report.error is None and report.duplicate_file is False
    assert [(s.account_number, s.status, s.gap, s.tx_count) for s in report.sections] == [
        ("00000000001", "ok", Decimal("0.00"), 3), ("00000000002", "ok", Decimal("0.00"), 1)]
    assert report.sections[0].statement_date == date(2024, 4, 10)
    sha = hashlib.sha256(data).hexdigest()
    assert (pdf_dir / f"{sha}.pdf").read_bytes() == data
    assert _count(conn) == 4
    assert len(rec.calls) == 1                     # une seule passe pour les deux sections
    assert sorted(rec.calls[0]) == [r[0] for r in conn.execute("SELECT id FROM transactions ORDER BY id")]

    row = conn.execute("SELECT op_date, value_date, label, label_norm, amount_cents, category_id, manual "
                       "FROM transactions ORDER BY id LIMIT 1").fetchone()
    assert row == ("2024-03-11", "2024-03-11", "PAIEMENT CB 0703 VILLEA MAGASIN ALPHA CARTE 0000",
                   "PAIEMENT CB VILLEA MAGASIN ALPHA", -250, None, 0)
    assert conn.execute("SELECT number, kind FROM accounts ORDER BY id").fetchall() == [
        ("00000000001", "checking"), ("00000000002", "savings")]
    doc = conn.execute("SELECT sha256, filename, parser, statement_date FROM documents").fetchone()
    assert doc == (sha, "a.pdf", "creditmutuel", "2024-04-10")
    assert not conn.in_transaction


def test_rank_among_exact_duplicates(tmp_path, conn, pdf_dir):
    section = FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "95,00"), [
        FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 CAFE GAMMA"], debit="2,50"),
        FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 CAFE GAMMA"], debit="2,50")])
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [section]), "a.pdf", pdf_dir, PARSERS, Recorder())
    keys = [r[0] for r in conn.execute("SELECT tx_key FROM transactions ORDER BY id")]
    norm = normalize_label("PAIEMENT CB 0703 CAFE GAMMA")
    assert keys == [tx_key("00000000001", date(2024, 3, 11), Decimal("-2.50"), norm, rank) for rank in (0, 1)]


def test_same_file_twice(tmp_path, conn, pdf_dir):
    data = _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()])
    import_pdf(conn, data, "a.pdf", pdf_dir, PARSERS, Recorder())
    rec = Recorder()
    report = import_pdf(conn, data, "copie.pdf", pdf_dir, PARSERS, rec)
    assert report.duplicate_file is True and report.sections == [] and report.error is None
    assert rec.calls == []
    assert _count(conn) == 4
    assert _count(conn, "SELECT COUNT(*) FROM documents") == 1
    assert _count(conn, "SELECT COUNT(*) FROM sections") == 2


def test_overlapping_section_identical(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    other = _pdf(tmp_path, "b.pdf", [_cc_section(), _livret_section()])     # fichier différent
    rec = Recorder()
    report = import_pdf(conn, other, "b.pdf", pdf_dir, PARSERS, rec)
    assert [(s.account_number, s.status) for s in report.sections] == [
        ("00000000001", "superseded"), ("00000000002", "ok")]
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE account_id = "
                        "(SELECT id FROM accounts WHERE number = '00000000001')") == 3
    assert _count(conn) == 4
    assert len(rec.calls) == 1 and len(rec.calls[0]) == 1


def test_overlapping_section_different(tmp_path, conn, pdf_dir):
    first = import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    old_section = first.sections[0].section_id
    changed = _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")])
    report = import_pdf(conn, changed, "b.pdf", pdf_dir, PARSERS, Recorder())
    [s] = report.sections
    assert s.status == "conflict" and s.tx_count == 3
    assert _count(conn) == 3
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE amount_cents = 100000") == 1

    rec = Recorder()
    resolve_conflict(conn, s.section_id, True, rec, pdf_dir=pdf_dir, parsers=PARSERS)
    assert not conn.in_transaction
    assert _statuses(conn) == ["superseded", "ok"]
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE section_id = ?", old_section) == 0
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE section_id = ?", s.section_id) == 3
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE amount_cents = 110000") == 1
    assert len(rec.calls) == 1 and len(rec.calls[0]) == 3


def test_resolve_conflict_refused(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")]),
                        "b.pdf", pdf_dir, PARSERS, Recorder())
    rec = Recorder()
    resolve_conflict(conn, report.sections[0].section_id, False, rec)
    assert _statuses(conn) == ["ok", "superseded"]
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE amount_cents = 100000") == 1
    assert rec.calls == []
    with pytest.raises(ValueError):
        resolve_conflict(conn, report.sections[0].section_id, True, rec, pdf_dir=pdf_dir, parsers=PARSERS)


def test_accept_conflict_requires_pdf_dir_and_parsers(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")]),
                        "b.pdf", pdf_dir, PARSERS, Recorder())
    with pytest.raises(ValueError):
        resolve_conflict(conn, report.sections[0].section_id, True, Recorder())
    assert _statuses(conn) == ["ok", "conflict"]


def test_accept_conflict_unbalanced_on_reparse_keeps_old_version(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    before = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")]),
                        "b.pdf", pdf_dir, PARSERS, Recorder())
    rec = Recorder()
    resolve_conflict(conn, report.sections[0].section_id, True, rec, pdf_dir=pdf_dir, parsers=[_BrokenParser()])
    assert not conn.in_transaction
    assert _statuses(conn) == ["ok", "error"]
    assert conn.execute("SELECT gap_cents FROM sections ORDER BY id").fetchall()[1] == (100,)
    assert conn.execute("SELECT * FROM transactions ORDER BY id").fetchall() == before
    assert rec.calls == []


def test_accept_conflict_identical_on_reparse_is_superseded(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    before = conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()
    # importé avec un parseur « régressé » (montant faussé mais équilibré) → conflit ;
    # le parseur corrigé relit un contenu identique à la version en place.
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section()], x_shift=10), "b.pdf", pdf_dir,
                        [_ShiftedParser()], Recorder())
    assert report.sections[0].status == "conflict"
    rec = Recorder()
    resolve_conflict(conn, report.sections[0].section_id, True, rec, pdf_dir=pdf_dir, parsers=PARSERS)
    assert _statuses(conn) == ["ok", "superseded"]
    assert conn.execute("SELECT * FROM transactions ORDER BY id").fetchall() == before
    assert rec.calls == []


def test_manual_override_survives_conflict_resolution(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    cat = _override(conn, "PRLV SEPA BETA")
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")]),
                        "b.pdf", pdf_dir, PARSERS, Recorder())
    resolve_conflict(conn, report.sections[0].section_id, True, Recorder(), pdf_dir=pdf_dir, parsers=PARSERS)
    assert conn.execute("SELECT category_id, manual FROM transactions WHERE label = 'PRLV SEPA BETA'").fetchone() == (cat, 1)


def test_unbalanced_section_rejected(tmp_path, conn, pdf_dir):
    bad = _cc_section(closing="1.090,00")          # attendu 1.087,50
    report = import_pdf(conn, _pdf(tmp_path, "a.pdf", [bad, _livret_section()]), "a.pdf", pdf_dir,
                        PARSERS, Recorder())
    assert [(s.account_number, s.status, s.gap) for s in report.sections] == [
        ("00000000001", "error", Decimal("2.50")), ("00000000002", "ok", Decimal("0.00"))]
    assert conn.execute("SELECT status, gap_cents FROM sections ORDER BY id").fetchall() == [("error", 250), ("ok", 0)]
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE section_id = ?", report.sections[0].section_id) == 0
    assert _count(conn) == 1


def test_not_a_pdf(conn, pdf_dir):
    rec = Recorder()
    report = import_pdf(conn, b"hello", "x.pdf", pdf_dir, PARSERS, rec)
    assert report.error and report.document_id is None and report.sections == []
    assert _count(conn, "SELECT COUNT(*) FROM documents") == 0
    assert not pdf_dir.exists() or list(pdf_dir.iterdir()) == []
    assert rec.calls == []


def test_unknown_pdf(tmp_path, conn, pdf_dir):
    other = tmp_path / "other.pdf"
    c = canvas.Canvas(str(other), pagesize=(595, 842))
    c.drawString(72, 770, "Facture fictive n° 42")
    c.showPage()
    c.save()
    report = import_pdf(conn, other.read_bytes(), "other.pdf", pdf_dir, PARSERS, Recorder())
    assert report.error and report.document_id is None
    assert _count(conn, "SELECT COUNT(*) FROM documents") == 0
    assert not pdf_dir.exists() or list(pdf_dir.iterdir()) == []


def test_parse_error_writes_nothing(tmp_path, conn, pdf_dir):
    no_section = tmp_path / "empty.pdf"
    c = canvas.Canvas(str(no_section), pagesize=(595, 842))
    c.drawString(72, 770, "RELEVE ET INFORMATIONS BANCAIRES")
    c.drawString(72, 750, "Débit Crédit 10 avril 2024")
    c.showPage()
    c.save()
    report = import_pdf(conn, no_section.read_bytes(), "empty.pdf", pdf_dir, PARSERS, Recorder())
    assert report.error
    assert _count(conn, "SELECT COUNT(*) FROM documents") == 0
    assert not pdf_dir.exists() or list(pdf_dir.iterdir()) == []


def test_failure_in_transaction_rolls_back_and_removes_file(tmp_path, conn, pdf_dir):
    def boom(conn, ids):
        raise RuntimeError("échec simulé")

    with pytest.raises(RuntimeError):
        import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, boom)
    assert not conn.in_transaction
    for table in ("documents", "sections", "transactions", "accounts"):
        assert _count(conn, f"SELECT COUNT(*) FROM {table}") == 0
    assert list(pdf_dir.iterdir()) == []


def test_tx_key_collision_with_other_statement_marks_only_that_section(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    # même compte, autre date de relevé, mais mêmes opérations : identités en collision
    data = make_statement(tmp_path / "b.pdf", "10 mai 2024", [_cc_section(), _livret_section()]).read_bytes()
    report = import_pdf(conn, data, "b.pdf", pdf_dir, PARSERS, Recorder())
    assert report.error is None and report.document_id is not None
    cc, livret = report.sections
    assert (cc.status, cc.gap) == ("error", Decimal("0.00"))
    assert "00000000001" in cc.message and "2024-04-10" in cc.message
    assert livret.status == "ok"
    assert _statuses(conn) == ["ok", "error", "ok"]
    assert _count(conn) == 4
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE section_id = ?", cc.section_id) == 0
    assert len(list(pdf_dir.iterdir())) == 2


def _override(conn, label):
    """Reclassement manuel de la transaction `label` (comme le fera l'interface)."""
    cat = conn.execute("INSERT INTO categories(name, kind) VALUES (?, 'expense')", (f"Test {label}",)).lastrowid
    key = conn.execute("SELECT tx_key FROM transactions WHERE label = ?", (label,)).fetchone()[0]
    conn.execute("INSERT INTO manual_overrides(tx_key, category_id) VALUES (?, ?)", (key, cat))
    conn.execute("UPDATE transactions SET category_id = ?, manual = 1 WHERE tx_key = ?", (cat, key))
    conn.commit()
    return cat


def test_manual_override_survives_reanalyse(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    cat = _override(conn, "PRLV SEPA BETA")

    rec = Recorder()
    [report] = reanalyse_all(conn, pdf_dir, PARSERS, rec)
    assert report.error is None
    assert [s.status for s in report.sections] == ["ok", "ok"]
    assert not conn.in_transaction
    new_ids = {r[0] for r in conn.execute("SELECT id FROM transactions")}
    assert len(new_ids) == 4 and len(rec.calls) == 1 and sorted(rec.calls[0]) == sorted(new_ids)
    rows = conn.execute("SELECT label, category_id, manual FROM transactions ORDER BY id").fetchall()
    assert ("PRLV SEPA BETA", cat, 1) in rows
    assert all((c, m) == (None, 0) for label, c, m in rows if label != "PRLV SEPA BETA")
    assert _statuses(conn) == ["ok", "ok"]


class _BrokenParser:
    """Parseur « régressé » : fausse le premier montant de la première section."""

    name = "creditmutuel"

    def can_parse(self, pdf_path):
        return CreditMutuelParser().can_parse(pdf_path)

    def parse(self, pdf_path):
        doc = CreditMutuelParser().parse(pdf_path)
        s = doc.sections[0]
        txs = [replace(s.transactions[0], amount=s.transactions[0].amount - 1), *s.transactions[1:]]
        doc.sections[0] = replace(s, transactions=txs)
        return doc


def test_reanalyse_keeps_old_version_when_section_now_fails(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    cc_rows = "SELECT * FROM transactions WHERE account_id = (SELECT id FROM accounts WHERE number = '00000000001') ORDER BY id"
    before = conn.execute(cc_rows).fetchall()
    assert len(before) == 3
    [report] = reanalyse_all(conn, pdf_dir, [_BrokenParser()], Recorder())
    assert report.error
    assert [(s.status, s.gap) for s in report.sections] == [("error", Decimal("1.00")), ("ok", Decimal("0.00"))]
    assert _statuses(conn) == ["ok", "ok"]
    assert conn.execute(cc_rows).fetchall() == before


def test_reanalyse_fixes_section_previously_in_error(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               [_BrokenParser()], Recorder())
    assert _statuses(conn) == ["error", "ok"]
    [report] = reanalyse_all(conn, pdf_dir, PARSERS, Recorder())
    assert [s.status for s in report.sections] == ["ok", "ok"]
    assert _statuses(conn) == ["ok", "ok"]
    assert _count(conn) == 4


class _ShiftedParser:
    """Parseur « régressé » mais équilibré : déplace 1,00 € d'une transaction à l'autre."""

    name = "creditmutuel"

    def can_parse(self, pdf_path):
        return CreditMutuelParser().can_parse(pdf_path)

    def parse(self, pdf_path):
        doc = CreditMutuelParser().parse(pdf_path)
        s = doc.sections[0]
        t0, t1, *rest = s.transactions
        txs = [replace(t0, amount=t0.amount - 1), replace(t1, amount=t1.amount + 1), *rest]
        doc.sections[0] = replace(s, transactions=txs)
        return doc


class _DroppingParser:
    """Parseur « régressé » : ne retrouve plus la deuxième section."""

    name = "creditmutuel"

    def can_parse(self, pdf_path):
        return CreditMutuelParser().can_parse(pdf_path)

    def parse(self, pdf_path):
        doc = CreditMutuelParser().parse(pdf_path)
        del doc.sections[1]
        return doc


def test_reanalyse_reports_ok_section_no_longer_found(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    livret_rows = "SELECT * FROM transactions WHERE account_id = (SELECT id FROM accounts WHERE number = '00000000002')"
    before = conn.execute(livret_rows).fetchall()
    assert len(before) == 1
    [report] = reanalyse_all(conn, pdf_dir, [_DroppingParser()], Recorder())
    assert "00000000002" in report.error
    assert [(s.account_number, s.status) for s in report.sections] == [
        ("00000000001", "ok"), ("00000000002", "error")]
    missing = report.sections[1]
    assert missing.message == "ancienne version conservée"
    assert missing.statement_date == date(2024, 4, 10) and missing.gap == Decimal("0.00")
    assert _statuses(conn) == ["ok", "ok"]
    assert conn.execute(livret_rows).fetchall() == before


def _section_row(conn, section_id):
    return conn.execute("SELECT status, gap_cents, message FROM sections WHERE id = ?", (section_id,)).fetchone()


def test_import_collision_message_is_stored(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    data = make_statement(tmp_path / "b.pdf", "10 mai 2024", [_cc_section()]).read_bytes()
    [cc] = import_pdf(conn, data, "b.pdf", pdf_dir, PARSERS, Recorder()).sections
    assert _section_row(conn, cc.section_id) == ("error", 0, cc.message)
    assert "00000000001" in cc.message


def test_collision_on_conflict_acceptance_stores_reason(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_section(closing="1.187,50", vir="1.100,00")]),
                        "b.pdf", pdf_dir, PARSERS, Recorder())
    conflict = report.sections[0].section_id
    # un autre relevé (autre date) contient déjà le virement de la nouvelle version
    other = FSection(CC, OPEN, ("CREDITEUR", "10/05/2024", "1.200,00"), [
        FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit="1.100,00")])
    data = make_statement(tmp_path / "c.pdf", "10 mai 2024", [other]).read_bytes()
    assert import_pdf(conn, data, "c.pdf", pdf_dir, PARSERS, Recorder()).sections[0].status == "ok"

    resolve_conflict(conn, conflict, True, Recorder(), pdf_dir=pdf_dir, parsers=PARSERS)
    assert _section_row(conn, conflict) == (
        "error", 0, "opérations déjà importées avec le relevé du compte 00000000001 au 2024-05-10")


def test_section_message_cleared_when_reanalysed_ok(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               [_BrokenParser()], Recorder())
    conn.execute("UPDATE sections SET message = 'ancien motif'")
    conn.commit()
    reanalyse_all(conn, pdf_dir, PARSERS, Recorder())
    assert [r[2] for r in conn.execute("SELECT status, gap_cents, message FROM sections ORDER BY id")] == [None, None]


def _cc_transfer(amount):
    closing = f"{100 - int(amount)},00"
    return FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", closing), [
        FTx("15/03/2024", "15/03/2024", ["VIR VERS LIVRET TEST"], debit=f"{amount},00")])


def test_former_mirror_is_reclassified_when_its_source_is_replaced(tmp_path, conn, pdf_dir):
    from app.classify.pipeline import on_new_transactions
    from app.classify.rules import create_rule
    savings = conn.execute("INSERT INTO categories(name, kind) VALUES ('Épargne', 'savings')").lastrowid
    create_rule(conn, "VIR VERS LIVRET", "contains", savings)
    conn.commit()
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_transfer("20"), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, on_new_transactions)
    livret = "SELECT category_id, rule_id, mirror_of IS NOT NULL FROM transactions WHERE label = 'VIR EPARGNE TEST'"
    assert conn.execute(livret).fetchone() == (savings, None, 1)        # miroir : catégorie de sa source

    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_cc_transfer("30")]), "b.pdf", pdf_dir, PARSERS,
                        on_new_transactions)
    resolve_conflict(conn, report.sections[0].section_id, True, on_new_transactions, pdf_dir=pdf_dir,
                     parsers=PARSERS)
    # la nouvelle source (30 €) ne s'apparie plus : l'ancien miroir est reclassé par les règles
    assert conn.execute(livret).fetchone() == (None, None, 0)


class _RelabelParser:
    """Parseur corrigé qui lit désormais un libellé plus complet pour le prélèvement."""

    name = "creditmutuel"

    def can_parse(self, pdf_path):
        return CreditMutuelParser().can_parse(pdf_path)

    def parse(self, pdf_path):
        doc = CreditMutuelParser().parse(pdf_path)
        s = doc.sections[0]
        txs = [replace(t, label="PRLV SEPA BETA ENERGIE") if t.label == "PRLV SEPA BETA" else t
               for t in s.transactions]
        doc.sections[0] = replace(s, transactions=txs)
        return doc


def test_manual_override_follows_label_change_on_reanalyse(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    cat = _override(conn, "PRLV SEPA BETA")
    [report] = reanalyse_all(conn, pdf_dir, [_RelabelParser()], Recorder())
    assert [s.orphaned_overrides for s in report.sections] == [0, 0]
    row = conn.execute("SELECT tx_key, category_id, manual FROM transactions "
                       "WHERE label = 'PRLV SEPA BETA ENERGIE'").fetchone()
    assert row[1:] == (cat, 1)
    assert conn.execute("SELECT tx_key FROM manual_overrides").fetchall() == [(row[0],)]


def test_unmappable_manual_override_is_counted_on_reanalyse(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    _override(conn, "PAIEMENT CB 0703 VILLEA MAGASIN ALPHA CARTE 0000")
    [report] = reanalyse_all(conn, pdf_dir, [_ShiftedParser()], Recorder())     # son montant change
    assert [s.status for s in report.sections] == ["ok", "ok"]
    assert [s.orphaned_overrides for s in report.sections] == [1, 0]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE manual = 1").fetchone()[0] == 0


def test_manual_override_follows_label_change_on_conflict_acceptance(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    cat = _override(conn, "PRLV SEPA BETA")
    relabelled = FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "1.087,50"), [
        FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 VILLEA", "MAGASIN ALPHA CARTE 0000"], debit="2,50"),
        FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit="1.000,00"),
        FTx("13/03/2024", "13/03/2024", ["PRLV SEPA BETA ENERGIE"], debit="10,00")])
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [relabelled]), "b.pdf", pdf_dir, PARSERS, Recorder())
    assert report.sections[0].status == "conflict"
    orphaned = resolve_conflict(conn, report.sections[0].section_id, True, Recorder(), pdf_dir=pdf_dir,
                                parsers=PARSERS)
    assert orphaned == 0
    assert conn.execute("SELECT category_id, manual FROM transactions "
                        "WHERE label = 'PRLV SEPA BETA ENERGIE'").fetchone() == (cat, 1)


def _twins(*labels):
    """Section de deux prélèvements de même date et même montant, dans l'ordre `labels`."""
    return FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "80,00"), [
        FTx("13/03/2024", "13/03/2024", [label], debit="10,00") for label in labels])


def test_carried_override_never_overwrites_an_occupied_key(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_twins("PRLV SEPA FOO", "PRLV SEPA BAR")]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    foo, bar = _override(conn, "PRLV SEPA FOO"), _override(conn, "PRLV SEPA BAR")
    # les deux opérations changent de rang ; FOO est relu autrement et prendrait le rang de BAR
    report = import_pdf(conn, _pdf(tmp_path, "b.pdf", [_twins("PRLV SEPA BAR", "PRLV SEPA FOO GAZ")]), "b.pdf",
                        pdf_dir, PARSERS, Recorder())
    assert report.sections[0].status == "conflict"
    orphaned = resolve_conflict(conn, report.sections[0].section_id, True, Recorder(), pdf_dir=pdf_dir,
                                parsers=PARSERS)
    assert orphaned == 1
    rows = conn.execute("SELECT label, category_id, manual FROM transactions ORDER BY id").fetchall()
    assert rows == [("PRLV SEPA BAR", bar, 1), ("PRLV SEPA FOO GAZ", None, 0)]
    assert foo in [r[0] for r in conn.execute("SELECT category_id FROM manual_overrides")]


def test_classification_runs_once_per_operation(tmp_path, conn, pdf_dir):
    """Le rappel de classement (une passe complète) est appelé une fois par import, acceptation de conflit
    ou ré-analyse, quel que soit le nombre de sections et de documents."""
    rec = Recorder()
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir, PARSERS, rec)
    assert len(rec.calls) == 1
    later_cc = FSection(CC, ("CREDITEUR", "10/04/2024", "1.087,50"), ("CREDITEUR", "10/05/2024", "1.077,50"),
                        [FTx("12/04/2024", "12/04/2024", ["PRLV SEPA GAMMA"], debit="10,00")])
    rec = Recorder()
    import_pdf(conn, make_statement(tmp_path / "b.pdf", "10 mai 2024", [later_cc]).read_bytes(), "b.pdf",
               pdf_dir, PARSERS, rec)
    assert len(rec.calls) == 1
    rec = Recorder()
    reports = reanalyse_all(conn, pdf_dir, PARSERS, rec)
    assert len(reports) == 2 and all(r.error is None for r in reports)
    assert len(rec.calls) == 1 and len(rec.calls[0]) == 5
    assert not conn.in_transaction
    rec = Recorder()
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "copie.pdf", pdf_dir,
               PARSERS, rec)
    assert rec.calls == []                         # rien d'inséré : pas de passe


class _FlakyClassifier:
    """Classement qui échoue `failures` fois (l'écriture faite avant l'échec doit être annulée)."""

    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    def __call__(self, conn, new_ids):
        self.calls += 1
        conn.execute("UPDATE transactions SET suggestion_reason = 'partiel'")
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked: libellé secret")


def test_reanalyse_final_pass_retried_once_and_logs_type_only(tmp_path, conn, pdf_dir, caplog):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    flaky = _FlakyClassifier(1)
    with caplog.at_level("WARNING"):
        [report] = reanalyse_all(conn, pdf_dir, PARSERS, flaky)
    assert report.error is None and flaky.calls == 2 and not conn.in_transaction
    assert "OperationalError" in caplog.text and "secret" not in caplog.text
    assert {r[0] for r in conn.execute("SELECT suggestion_reason FROM transactions")} == {"partiel"}


def test_reanalyse_final_pass_reraises_second_failure(tmp_path, conn, pdf_dir):
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
               PARSERS, Recorder())
    flaky = _FlakyClassifier(2)
    with pytest.raises(sqlite3.OperationalError):
        reanalyse_all(conn, pdf_dir, PARSERS, flaky)
    assert flaky.calls == 2 and not conn.in_transaction
    assert {r[0] for r in conn.execute("SELECT suggestion_reason FROM transactions")} == {None}


def _api_tx(conn, account_number, day):
    from app.sources.base import AccountFetch, FetchedTransaction
    from app.sources.service import store_fetch
    account_id = repo_upsert(conn, account_number)
    store_fetch(conn, "enablebanking", AccountFetch(account_id, [
        FetchedTransaction(date.fromisoformat(day), None, "CB API FICTIVE", Decimal("-1.00"))]))
    conn.commit()


def repo_upsert(conn, number):
    from app.store import repo
    return repo.upsert_account(conn, number, "C/C EUROCOMPTE TEST")


def test_pdf_period_already_covered_by_bank_sync_is_error(tmp_path, conn, pdf_dir):
    _api_tx(conn, "00000000001", "2024-04-01")      # dans la période du relevé du 10/04/2024
    report = import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section(), _livret_section()]), "a.pdf", pdf_dir,
                        PARSERS, Recorder())
    statuses = {s.account_number: (s.status, s.message) for s in report.sections}
    assert statuses["00000000001"] == ("error", "période déjà couverte par la synchronisation bancaire")
    assert statuses["00000000002"] == ("ok", None)
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE source = 'pdf'") == 1


def test_pdf_before_first_bank_operation_is_imported(tmp_path, conn, pdf_dir):
    _api_tx(conn, "00000000001", "2024-04-11")      # après le relevé du 10/04/2024
    report = import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    assert [s.status for s in report.sections] == ["ok"]


def test_reanalyse_keeps_api_overlap_guard(tmp_path, conn, pdf_dir):
    _api_tx(conn, "00000000001", "2024-04-01")
    import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    reports = reanalyse_all(conn, pdf_dir, PARSERS, Recorder())
    assert [(s.status, s.message) for r in reports for s in r.sections] == \
        [("error", "période déjà couverte par la synchronisation bancaire")]
    assert _count(conn, "SELECT COUNT(*) FROM transactions WHERE source = 'pdf'") == 0


def test_pdf_fills_hole_between_synced_periods(tmp_path, conn, pdf_dir):
    _api_tx(conn, "00000000001", "2024-01-15")      # synchro ancienne, puis consentement expiré
    _api_tx(conn, "00000000001", "2024-05-02")      # reprise après le trou
    report = import_pdf(conn, _pdf(tmp_path, "a.pdf", [_cc_section()]), "a.pdf", pdf_dir, PARSERS, Recorder())
    assert [s.status for s in report.sections] == ["ok"]
