from datetime import date, timedelta
from decimal import Decimal

import pytest
from reportlab.pdfgen import canvas

from app.importer.creditmutuel import CreditMutuelParser, _columns, _Line
from app.model import ParseError
from pdfgen import FSection, FTx, make_statement

CC = "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"
LIVRET = "LIVRET BLEU TEST N° 00000000002 en euros"
OPEN = ("CREDITEUR", "11/03/2024", "100,00")


def _parse(tmp_path, sections, **kwargs):
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", sections, **kwargs)
    return CreditMutuelParser().parse(pdf)


def _three_txs():
    return [FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB ALPHA", "VILLEA CARTE 0000"], debit="2,50"),
            FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit="1.000,00"),
            FTx("13/03/2024", "13/03/2024", ["PRLV SEPA BETA"], debit="10,00")]


def _two_accounts():
    return [FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "1.087,50"), _three_txs()),
            FSection(LIVRET, ("CREDITEUR", "11/03/2024", "500,00"), ("CREDITEUR", "10/04/2024", "520,00"),
                     [FTx("15/03/2024", "15/03/2024", ["VIR EPARGNE TEST"], credit="20,00")])]


def _many_txs(n):
    start = date(2024, 3, 11)
    txs = []
    for i in range(n):
        d = (start + timedelta(days=i % 28)).strftime("%d/%m/%Y")
        txs.append(FTx(d, d, [f"PAIEMENT CB {i:03d}", "MAGASIN TEST"], debit=f"{i + 1},00"))
    return txs


def test_two_accounts_one_pdf(tmp_path):
    doc = _parse(tmp_path, _two_accounts())
    assert [(s.account_label, s.account_number) for s in doc.sections] == [
        ("C/C EUROCOMPTE TEST", "00000000001"), ("LIVRET BLEU TEST", "00000000002")]
    cc, livret = doc.sections
    assert [t.amount for t in cc.transactions] == [Decimal("-2.50"), Decimal("1000.00"), Decimal("-10.00")]
    assert (cc.opening, cc.closing) == (Decimal("100.00"), Decimal("1087.50"))
    assert [(t.label, t.amount) for t in livret.transactions] == [("VIR EPARGNE TEST", Decimal("20.00"))]
    assert (livret.opening, livret.closing) == (Decimal("500.00"), Decimal("520.00"))
    assert all(s.statement_date == date(2024, 4, 10) for s in doc.sections)


def test_multipage_section(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"),   # 1.830 = 1 + 2 + … + 60
                                     ("CREDITEUR", "10/04/2024", "0,00"), _many_txs(60))], rows_per_page=25)
    assert len(doc.sections) == 1
    s = doc.sections[0]
    assert len(s.transactions) == 60
    assert [t.label for t in s.transactions] == [f"PAIEMENT CB {i:03d} MAGASIN TEST" for i in range(60)]
    assert [t.amount for t in s.transactions] == [Decimal(-(i + 1)) for i in range(60)]
    assert s.opening - sum(-t.amount for t in s.transactions) == s.closing


def test_two_accounts_across_pages(tmp_path):
    sections = _two_accounts()
    sections[0].txs = _many_txs(20)
    doc = _parse(tmp_path, sections, rows_per_page=12)
    assert [s.account_number for s in doc.sections] == ["00000000001", "00000000002"]
    assert len(doc.sections[0].transactions) == 20
    assert [t.label for t in doc.sections[1].transactions] == ["VIR EPARGNE TEST"]


def test_amount_on_continuation_line(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "70,00"), [
        FTx("11/03/2024", "11/03/2024", ["VIR SEPA X", "MOTIF Y"], debit="30,00", amount_line=1)])])
    [tx] = doc.sections[0].transactions
    assert (tx.label, tx.amount) == ("VIR SEPA X MOTIF Y", Decimal("-30.00"))


def test_debiteur_balance(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, ("DEBITEUR", "11/03/2024", "50,00"), ("CREDITEUR", "10/04/2024", "50,00"),
                                     [FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit="100,00")])])
    s = doc.sections[0]
    assert s.opening == Decimal("-50.00")
    assert s.closing == Decimal("50.00")


def test_debiteur_closing_balance(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, OPEN, ("DEBITEUR", "10/04/2024", "1.200,00"),
                                     [FTx("12/03/2024", "12/03/2024", ["PRLV TEST"], debit="1.300,00")])])
    assert doc.sections[0].closing == Decimal("-1200.00")


def test_empty_section(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "100,00"), [])])
    s = doc.sections[0]
    assert s.transactions == []
    assert (s.opening, s.closing) == (Decimal("100.00"), Decimal("100.00"))


@pytest.mark.parametrize("amount", ["+1.674,99", "1.674,99"])
def test_other_accounts_block_ignored(tmp_path, amount):
    doc = _parse(tmp_path, _two_accounts(), other_accounts=[("00000000003 LIVRET TEST", amount)])
    assert [s.account_number for s in doc.sections] == ["00000000001", "00000000002"]
    assert [len(s.transactions) for s in doc.sections] == [3, 1]
    labels = " ".join(t.label for s in doc.sections for t in s.transactions)
    assert "LIVRET TEST" not in labels and "SITUATION" not in labels


def test_amounts_not_drawn_last(tmp_path):
    first = _parse(tmp_path, _two_accounts(), amounts_drawn_last=True)
    second = _parse(tmp_path, _two_accounts(), amounts_drawn_last=False)
    assert first == second


def test_page_footer_not_in_labels(tmp_path):
    doc = _parse(tmp_path, [FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                                     _many_txs(60))], rows_per_page=25)
    assert not any("Page" in t.label for t in doc.sections[0].transactions)


def test_balance_preceded_by_other_words(tmp_path):
    pdf = tmp_path / "s.pdf"
    make_statement(pdf, "10 avril 2024", [FSection(CC, OPEN, ("CREDITEUR", "10/04/2024", "97,50"),
                                                   [FTx("11/03/2024", "11/03/2024", ["PRLV TEST"], debit="2,50")])],
                   balance_prefix="NOUVEAU")
    s = CreditMutuelParser().parse(pdf).sections[0]
    assert (s.opening, s.closing, len(s.transactions)) == (Decimal("100.00"), Decimal("97.50"), 1)


# --- Repérage des colonnes (addendum 2) ---------------------------------------------------------

def test_columns_found_without_account_number_on_repeated_header(tmp_path):
    section = FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                       _many_txs(60), repeat_header="C/C EUROCOMPTE TEST (suite)")
    doc = _parse(tmp_path, [section], rows_per_page=25)
    assert len(doc.sections) == 1
    assert len(doc.sections[0].transactions) == 60


def test_transaction_lines_without_columns_raise(tmp_path):
    section = FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                       _many_txs(60))
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", [section], rows_per_page=25,
                         repeat_columns=False)
    with pytest.raises(ParseError, match="en-tête de compte/colonnes introuvables"):
        CreditMutuelParser().parse(pdf)


def test_page_without_header_nor_columns_raises(tmp_path):
    """Page d'une section ouverte sans en-tête reconnu ni colonnes : erreur, pas de perte."""
    section = FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                       _many_txs(60), repeat_header="SUITE DU RELEVE")
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", [section], rows_per_page=25,
                         repeat_columns=False)
    with pytest.raises(ParseError, match="en-tête de compte/colonnes introuvables sur une page du "
                                         "compte 00000000001"):
        CreditMutuelParser().parse(pdf)


def _words(*specs):
    """Mots synthétiques (texte, x0) sur une même ligne, largeur 5 pt par caractère."""
    return _Line(100, [{"text": t, "x0": x, "x1": x + 5 * len(t), "top": 100} for t, x in specs])


@pytest.mark.parametrize("line", [
    _words(("Caisse", 320), ("de", 360), ("Crédit", 380), ("Mutuel", 420), ("débit", 460), ("Débit", 500)),
    _words(("Crédit", 400), ("Mutuel", 440), ("Débit", 480), ("différé", 520)),
    _words(("Débit", 100), ("EUROS", 130), ("Crédit", 170), ("EUROS", 205)),          # moitié gauche
    _words(("Crédit", 409), ("EUROS", 440), ("Débit", 480), ("EUROS", 515)),          # ordre inversé
    _words(("Débit", 409), ("EUROS", 440), ("Crédit", 480), ("Mutuel", 515)),         # sans EUROS
])
def test_boilerplate_does_not_install_columns(line):
    assert _columns(line, 595) is None


def test_columns_from_debit_euros_credit_euros():
    cols = _columns(_words(("KV.0000.000", 10), ("Débit", 409), ("EUROS", 440), ("Crédit", 480),
                           ("EUROS", 515)), 595)
    assert (cols.debit_x0, cols.credit_x0) == (409, 480)


@pytest.mark.parametrize("columns_offset", [-5, 0, 5])
def test_columns_line_separate_from_account_header(tmp_path, columns_offset):
    sections = _two_accounts()
    sections[0].txs = _many_txs(20)
    doc = _parse(tmp_path, sections, rows_per_page=12, columns_offset=columns_offset)
    assert [(s.account_label, s.account_number) for s in doc.sections] == [
        ("C/C EUROCOMPTE TEST", "00000000001"), ("LIVRET BLEU TEST", "00000000002")]
    assert [len(s.transactions) for s in doc.sections] == [20, 1]


# --- Date du relevé (addendum 1) ----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [("10 avril 2024", date(2024, 4, 10)),
                                            ("5 mars 2024", date(2024, 3, 5)),
                                            ("Relevé du 1 décembre 2023", date(2023, 12, 1))])
def test_statement_date_formats(tmp_path, text, expected):
    pdf = make_statement(tmp_path / "s.pdf", text, [FSection(CC, OPEN, OPEN, [])])
    assert CreditMutuelParser().parse(pdf).statement_date == expected


def test_uppercase_statement_date(tmp_path):
    pdf = make_statement(tmp_path / "s.pdf", "10 AVRIL 2024", [FSection(CC, OPEN, OPEN, [])])
    assert CreditMutuelParser().parse(pdf).statement_date == date(2024, 4, 10)


def test_later_uppercase_month_does_not_override_statement_date(tmp_path):
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", [FSection(CC, OPEN, OPEN, [])],
                         other_accounts=[("LIVRET TEST AU 5 MARS 2024", "1,00")])
    assert CreditMutuelParser().parse(pdf).statement_date == date(2024, 4, 10)


def test_impossible_statement_date_raises_parse_error(tmp_path):
    pdf = make_statement(tmp_path / "s.pdf", "31 avril 2024", [FSection(CC, OPEN, OPEN, [])])
    with pytest.raises(ParseError, match="date du relevé"):
        CreditMutuelParser().parse(pdf)


# --- Références techniques en marge (addendum 4) -------------------------------------------------

@pytest.mark.parametrize("columns_offset", [0, 5])
def test_margin_refs_never_in_labels(tmp_path, columns_offset):
    """Références en marge sur toutes les lignes (en-tête, colonnes, titres, tableau, pied)."""
    sections = _two_accounts()
    sections[0].txs = _many_txs(20)
    doc = _parse(tmp_path, sections, rows_per_page=12, margin_refs=True, columns_offset=columns_offset,
                 footer_lines=["Caisse de Credit Mutuel TEST"])
    assert [s.account_label for s in doc.sections] == ["C/C EUROCOMPTE TEST", "LIVRET BLEU TEST"]
    labels = [t.label for s in doc.sections for t in s.transactions]
    assert len(labels) == 21
    assert not any("KV." in label for label in labels)
    assert labels[0] == "PAIEMENT CB 000 MAGASIN TEST"


# --- Pied de page (fix 1 : C1) ---------------------------------------------------------------

def test_footer_boilerplate_not_in_labels(tmp_path):
    footer = ["Banque TEST societe cooperative", "ORIAS 00000000 mentions legales"]   # hors mots-clés
    doc = _parse(tmp_path, [FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                                     _many_txs(60))], rows_per_page=25, footer_lines=footer)
    labels = [t.label for t in doc.sections[0].transactions]
    assert labels == [f"PAIEMENT CB {i:03d} MAGASIN TEST" for i in range(60)]


def test_footer_keywords_stop_label_even_when_close(tmp_path):
    footer = ["Sous réserve de bonne fin", "www.banque-test.example", "Médiateur TEST"]
    doc = _parse(tmp_path, [FSection(CC, ("CREDITEUR", "11/03/2024", "1.830,00"), ("CREDITEUR", "10/04/2024", "0,00"),
                                     _many_txs(60))], rows_per_page=25, footer_lines=footer, footer_gap=12)
    labels = [t.label for t in doc.sections[0].transactions]
    assert labels == [f"PAIEMENT CB {i:03d} MAGASIN TEST" for i in range(60)]


# --- Garde-fous du générateur (addendum 3) --------------------------------------------------------

@pytest.mark.parametrize("tx", [FTx("11/03/2024", "11/03/2024", [], debit="1,00"),
                                FTx("11/03/2024", "11/03/2024", ["A"], debit="1,00", amount_line=1),
                                FTx("11/03/2024", "11/03/2024", ["A"], debit="1,00", amount_line=-1)])
def test_pdfgen_rejects_invalid_fixture(tmp_path, tx):
    with pytest.raises(AssertionError):
        make_statement(tmp_path / "s.pdf", "10 avril 2024", [FSection(CC, OPEN, OPEN, [tx])])


# --- can_parse (addendum 5) -----------------------------------------------------------------------

def test_can_parse_rejects_title_without_columns(tmp_path):
    pdf = tmp_path / "t.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(595, 842))
    c.setFont("Helvetica", 11)
    c.drawString(40, 790, "RELEVE ET INFORMATIONS BANCAIRES")
    c.drawString(40, 760, "10 avril 2024")
    c.showPage()
    c.save()
    assert CreditMutuelParser().can_parse(pdf) is False
