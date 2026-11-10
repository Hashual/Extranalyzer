import importlib.util
import re
from pathlib import Path

from pdfgen import FSection, FTx, make_statement

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_real_statements.py"
spec = importlib.util.spec_from_file_location("check_real_statements", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

CC = "C/C EUROCOMPTE TEST N° 00000000001 en euros (GD)"
LIVRET = "LIVRET BLEU TEST N° 00000000002 en euros"


def _good(date_open, date_close, label):
    return [
        FSection(CC, ("CREDITEUR", date_open, "100,00"), ("CREDITEUR", date_close, "97,50"),
                 [FTx("11/03/2024", "11/03/2024", [label, "MAGASIN ALPHA CARTE 0000"], debit="2,50"),
                  FTx("12/03/2024", "12/03/2024", ["INFO FICTIVE SANS MONTANT"])]),
        FSection(LIVRET, ("CREDITEUR", date_open, "500,00"), ("CREDITEUR", date_close, "520,00"),
                 [FTx("15/03/2024", "15/03/2024", ["VIR EPARGNE TEST"], credit="20,00")]),
    ]


def _out(capsys):
    return capsys.readouterr().out


def test_counters_on_synthetic_folder(tmp_path, capsys):
    folder = tmp_path / "rel"
    folder.mkdir()
    make_statement(folder / "a_secret.pdf", "10 avril 2024",
                   _good("11/03/2024", "10/04/2024", "PAIEMENT CB 0703 VILLEA"))
    (folder / "b_copy.pdf").write_bytes((folder / "a_secret.pdf").read_bytes())    # doublon exact
    unbalanced = [FSection(CC, ("CREDITEUR", "11/05/2024", "100,00"), ("CREDITEUR", "10/06/2024", "50,00"),
                           [FTx("12/05/2024", "12/05/2024", ["PRLV SEPA GAMMA"], debit="2,50")])]
    make_statement(folder / "c.pdf", "10 juin 2024", unbalanced)

    code = script.main([str(folder)])
    out = _out(capsys)
    values = dict(line.split(" : ") for line in out.splitlines() if " : " in line)

    assert code == 1                                  # une section error
    assert values["Fichiers PDF"] == "3"
    assert values["Fichiers en double"] == "1"
    assert values["Documents importés"] == "2"
    assert values["Sections ok"] == "2"
    assert values["Sections error"] == "1"
    assert values["Sections superseded"] == "0" and values["Sections conflict"] == "0"
    assert values["Transactions"] == "2"
    assert values["Transactions à classer"] in {"0", "1", "2"}
    assert values["Erreurs d'import"] == "0"
    assert values["Lignes d'information"] == "1"      # une seule version du doublon est comptée
    # confidentialité : aucun libellé, nom de fichier, numéro de compte, montant ni date
    for forbidden in ("PAIEMENT", "MAGASIN", "EPARGNE", "GAMMA", "secret", "copy", "00000000001",
                      "2,50", "100,00", "2024", "EUROCOMPTE"):
        assert forbidden not in out
    assert all(len(d) < 3 for d in re.findall(r"\d+", out))


def test_clean_folder_exits_zero(tmp_path, capsys):
    folder = tmp_path / "rel"
    folder.mkdir()
    make_statement(folder / "a.pdf", "10 avril 2024", _good("11/03/2024", "10/04/2024", "PRLV SEPA BETA"))
    assert script.main([str(folder)]) == 0
    assert "Sections error : 0" in _out(capsys)


def test_unparseable_pdf_counts_import_error_with_masked_kind(tmp_path, capsys):
    folder = tmp_path / "rel"
    folder.mkdir()
    (folder / "bad.pdf").write_bytes(b"%PDF-1.4 not a statement")
    assert script.main([str(folder)]) == 1
    out = _out(capsys)
    assert "Erreurs d'import : 1" in out and "bad" not in out


def test_usage_error(capsys):
    assert script.main([]) == 2
