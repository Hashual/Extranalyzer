from datetime import date
from decimal import Decimal

import pytest
from reportlab.pdfgen import canvas

from app.importer.creditmutuel import CreditMutuelParser
from app.model import ParseError
from pdfgen import FSection, FTx, make_statement


@pytest.mark.parametrize("x_shift", [0, 10])
def test_single_section(tmp_path, x_shift):
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", [FSection(
        header="C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)",
        opening=("CREDITEUR", "11/03/2024", "100,00"), closing=("CREDITEUR", "10/04/2024", "1.087,50"),
        txs=[FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB 0703 VILLEA", "MAGASIN ALPHA CARTE 0000"], debit="2,50"),
             FTx("12/03/2024", "12/03/2024", ["VIR DE M TEST"], credit="1.000,00"),
             FTx("13/03/2024", "13/03/2024", ["LOISIR BETA 14/03/2024 19,99"], debit="10,00")])], x_shift=x_shift)
    doc = CreditMutuelParser().parse(pdf)
    s = doc.sections[0]
    assert doc.statement_date == date(2024, 4, 10)
    assert (s.account_number, s.opening, s.closing) == ("00000000001", Decimal("100.00"), Decimal("1087.50"))
    assert [t.amount for t in s.transactions] == [Decimal("-2.50"), Decimal("1000.00"), Decimal("-10.00")]
    assert s.transactions[0].label == "PAIEMENT CB 0703 VILLEA MAGASIN ALPHA CARTE 0000"
    assert s.transactions[2].label == "LOISIR BETA 14/03/2024 19,99"     # Review Focus 1


def test_can_parse_rejects_other_pdf(tmp_path):
    other_pdf = tmp_path / "other.pdf"
    c = canvas.Canvas(str(other_pdf), pagesize=(595, 842))
    c.setFont("Helvetica", 12)
    c.drawString(72, 770, "Facture fictive n° 42 - Débit et Crédit sans rapport")
    c.showPage()
    c.save()
    assert CreditMutuelParser().can_parse(other_pdf) is False


def test_can_parse_accepts_statement(tmp_path):
    pdf = make_statement(tmp_path / "s.pdf", "10 avril 2024", [FSection(
        header="C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)",
        opening=("CREDITEUR", "11/03/2024", "100,00"), closing=("CREDITEUR", "10/04/2024", "100,00"),
        txs=[])])
    assert CreditMutuelParser().can_parse(pdf) is True


def test_parse_rejects_document_without_section(tmp_path):
    other_pdf = tmp_path / "other.pdf"
    c = canvas.Canvas(str(other_pdf), pagesize=(595, 842))
    c.setFont("Helvetica", 12)
    c.drawString(72, 770, "RELEVE ET INFORMATIONS BANCAIRES")
    c.drawString(72, 750, "10 avril 2024")
    c.showPage()
    c.save()
    with pytest.raises(ParseError, match="aucune section"):
        CreditMutuelParser().parse(other_pdf)
