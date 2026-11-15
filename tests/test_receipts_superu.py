from datetime import datetime

import pytest

from app.receipts import PARSERS_RECEIPTS
from app.receipts.model import ReceiptParseError
from app.receipts.service import ingest_file
from app.receipts.superu import SuperUParser, payment_method
from app.store.db import connect
from pdfgen import FSection, FTx, make_statement
from receiptgen import Discount, Item, make_superu_receipt, make_superu_slip

BASIC = [("FRUITS TEST", [Item("POMMES FICTIVES", 300, 2, 150), Item("POIRES FICTIVES", 189)]),
         ("EPICERIE TEST", [Item("PATES FICTIVES 500G", 95)])]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def rows(parsed):
    return [(x.aisle, x.label, x.quantity, x.unit_cents, x.amount_cents, x.kind) for x in parsed.lines]


def test_parse_basic_receipt(tmp_path):
    p = SuperUParser().parse(make_superu_receipt(tmp_path / "t.pdf", aisles=BASIC))
    assert p.shop == "MAGASIN TEST" and p.purchased_at == datetime(2025, 1, 8, 15, 10, 1)
    assert rows(p) == [("FRUITS TEST", "POMMES FICTIVES", 2, 150, 300, "item"),
                       ("FRUITS TEST", "POIRES FICTIVES", 1, 189, 189, "item"),
                       ("EPICERIE TEST", "PATES FICTIVES 500G", 1, 95, 95, "item")]
    assert p.total_cents == sum(x.amount_cents for x in p.lines) == 584
    assert [(x.method, x.amount_cents) for x in p.payments] == [("card", 584)]


def test_weighed_item_and_discount_lines(conn, tmp_path):
    aisles = [("LEGUMES TEST", [Item("CAROTTE FICTIVE", 106, 0.532, 199, weighed=True)]),
              ("EPICERIE TEST", [Item("BISCUITS FICTIFS", 900, 3, 300)])]
    pdf = make_superu_receipt(tmp_path / "t.pdf", aisles=aisles, discounts=[Discount("LOT BISCUITS TEST", 267, 3)])
    p = SuperUParser().parse(pdf)
    assert rows(p) == [("LEGUMES TEST", "CAROTTE FICTIVE", 0.532, 199, 106, "item"),
                       ("EPICERIE TEST", "BISCUITS FICTIFS", 3, 300, 900, "item"),
                       ("REMISES", "LOT BISCUITS TEST", 3, -89, -267, "discount")]
    assert p.total_cents == 739 and [(x.method, x.amount_cents) for x in p.payments] == [("card", 739)]
    r = ingest_file(conn, pdf.read_bytes(), "t.pdf", tmp_path / "r", PARSERS_RECEIPTS, origin="upload")
    assert (r.outcome, r.status) == ("stored", "ok")


def test_payments_split_card_and_meal_voucher(tmp_path):
    pdf = make_superu_receipt(tmp_path / "t.pdf", aisles=[("EPICERIE TEST", [Item("PANIER FICTIF", 1734)])],
                              payments=[("CB", 1234), ("TITRE RESTAURANT", 500)])
    p = SuperUParser().parse(pdf)
    # la ligne récapitulative « Payé en TITRES RESTAURANT » n'est pas un second paiement
    assert [(x.method, x.amount_cents) for x in p.payments] == [("card", 1234), ("meal_voucher", 500)]


def test_cash_with_change_is_net(tmp_path):
    pdf = make_superu_receipt(tmp_path / "t.pdf", aisles=[("EPICERIE TEST", [Item("PANIER FICTIF", 1650)])],
                              payments=[("ESPECES", 2000)], change_cents=350)
    assert [(x.method, x.amount_cents) for x in SuperUParser().parse(pdf).payments] == [("cash", 1650)]


def test_kind_detects_receipt_and_slip(tmp_path):
    parser = SuperUParser()
    for layout in ("omnipos", "classic"):
        assert parser.kind(make_superu_receipt(tmp_path / f"{layout}.pdf", layout=layout, aisles=BASIC)) == "receipt"
    assert parser.kind(make_superu_slip(tmp_path / "s1.pdf", kind="card", amount_cents=584)) == "cb_slip"
    assert parser.kind(make_superu_slip(tmp_path / "s2.pdf", kind="meal_voucher", amount_cents=500)) == "cb_slip"
    statement = make_statement(tmp_path / "releve.pdf", "10 avril 2024", [FSection(
        header="C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)",
        opening=("CREDITEUR", "11/03/2024", "100,00"), closing=("CREDITEUR", "10/04/2024", "97,50"),
        txs=[FTx("11/03/2024", "11/03/2024", ["PAIEMENT CB MAGASIN ALPHA"], debit="2,50")])])
    assert parser.kind(statement) is None
    garbage = tmp_path / "x.pdf"
    garbage.write_bytes(b"pas un pdf")
    assert parser.kind(garbage) is None


def test_total_mismatch_marks_receipt_error(conn, tmp_path):
    pdf = make_superu_receipt(tmp_path / "t.pdf", aisles=BASIC, total_cents=684, payments=[("CB", 684)])
    r = ingest_file(conn, pdf.read_bytes(), "t.pdf", tmp_path / "r", PARSERS_RECEIPTS, origin="upload")
    assert (r.outcome, r.status) == ("stored", "error") and "écart de" in r.message
    assert conn.execute("SELECT COUNT(*) FROM receipt_lines").fetchone()[0] == 0


def test_classic_layout(tmp_path):
    """Ancienne caisse : `>>>>` devant les rayons, heure sans secondes, quantité et pesée sous le libellé."""
    aisles = [("CREMERIE TEST", [Item("YAOURT FICTIF X8", 384, 2, 192), Item("BEURRE FICTIF", 275)]),
              ("LEGUMES TEST", [Item("TOMATE FICTIVE", 129, 0.432, 299, weighed=True)])]
    pdf = make_superu_receipt(tmp_path / "t.pdf", layout="classic", shop="Magasin Test",
                              when=datetime(2024, 11, 13, 18, 20), aisles=aisles,
                              discounts=[Discount("LOT YAOURTS TEST", 50)],
                              payments=[("BON DE REDUCTION", 30), ("PAIEMENT € CARTE U", 100),
                                        ("CB SANS CONTACT VX EUR", 608)])
    p = SuperUParser().parse(pdf)
    assert p.shop == "MAGASIN TEST" and p.purchased_at == datetime(2024, 11, 13, 18, 20)
    assert rows(p) == [("CREMERIE TEST", "YAOURT FICTIF X8", 2, 192, 384, "item"),
                       ("CREMERIE TEST", "BEURRE FICTIF", 1, 275, 275, "item"),
                       ("LEGUMES TEST", "TOMATE FICTIVE", 0.432, 299, 129, "item"),
                       ("REMISES", "LOT YAOURTS TEST", 1, -50, -50, "discount")]
    assert p.total_cents == 738
    assert [(x.method, x.amount_cents) for x in p.payments] == [("voucher", 30), ("voucher", 100), ("card", 608)]


def test_classic_layout_without_aisles_and_with_dot_decimals(tmp_path):
    """Caisse automatique : aucun titre de rayon, point décimal ; les articles vont dans « SANS RAYON »."""
    pdf = make_superu_receipt(tmp_path / "t.pdf", layout="classic", sep=".",
                              aisles=[(None, [Item("BAGUETTE FICTIVE", 59), Item("SODA FICTIF 1,5L", 202)])],
                              payments=[("ESPECES", 1000)], change_cents=739)
    p = SuperUParser().parse(pdf)
    assert rows(p) == [("SANS RAYON", "BAGUETTE FICTIVE", 1, 59, 59, "item"),
                       ("SANS RAYON", "SODA FICTIF 1,5L", 1, 202, 202, "item")]
    assert p.total_cents == 261 and [(x.method, x.amount_cents) for x in p.payments] == [("cash", 261)]


@pytest.mark.parametrize("label, method", [
    ("CB", "card"), ("CB SANS CONTACT", "card"), ("CARTE BANCAIRE EUR", "card"),
    ("TITRE RESTAURANT", "meal_voucher"), ("TRD EDENRED", "meal_voucher"), ("CHEQUE BIMPLI", "meal_voucher"),
    ("CARTE SWILE", "meal_voucher"),
    ("BON DE REDUCTION", "voucher"), ("BONS U (L)", "voucher"), ("PAIEMENT € CARTE U", "voucher"),
    ("ESPECES", "cash"), ("CHEQUE", "other"), ("MOYEN INCONNU", "other"),
])
def test_payment_method(label, method):
    assert payment_method(label) == method


def test_unreadable_receipt_raises(tmp_path):
    pdf = make_superu_slip(tmp_path / "s.pdf")
    with pytest.raises(ReceiptParseError):
        SuperUParser().parse(pdf)
