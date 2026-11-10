from app.classify.normalize import normalize_label


def test_normalize_label_plan_example():
    assert normalize_label("PAIEMENT CB 1203 VILLEA MAGASIN ALPHA CARTE 0000") == "PAIEMENT CB VILLEA MAGASIN ALPHA"


def test_normalize_label_uppercases_and_squeezes_spaces():
    assert normalize_label("  vir  de   m test ") == "VIR DE M TEST"


def test_normalize_label_removes_dates_and_long_references():
    assert normalize_label("PRLV SEPA BETA 14/03/2024 REF AB12CD34EF") == "PRLV SEPA BETA REF"


def test_normalize_label_keeps_short_or_digit_poor_tokens():
    # < 8 caractères, ou moins de 4 chiffres : conservés
    assert normalize_label("LOISIR AB1234 ABCDEFG12") == "LOISIR AB1234 ABCDEFG12"


def test_normalize_label_psc_and_mob():
    assert normalize_label("PAIEMENT PSC 0412 BOUTIQUE") == "PAIEMENT PSC BOUTIQUE"
    assert normalize_label("PAIEMENT MOB 0412 BOUTIQUE") == "PAIEMENT MOB BOUTIQUE"
