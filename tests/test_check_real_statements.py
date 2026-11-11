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
    for key in ("Suggestions vers Épargne", "Suggestions vers une autre catégorie",
                "Suggestions à une seule opération similaire", "Durée d'un reclassement en régime établi (ms)"):
        assert values[key].isdigit()
    assert int(values["Suggestions à une seule opération similaire"]) <= (
        int(values["Suggestions vers Épargne"]) + int(values["Suggestions vers une autre catégorie"]))
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
    assert "  type d'erreur : " in out
    kind = re.search(r"type d'erreur : (.*) x1", out).group(1)
    assert kind in (*script.KINDS, "autre")


def test_error_kind_is_allowlisted():
    assert script._kind("PDF illisible : secret 123") == "PDF illisible"
    assert script._kind("ligne de solde sans montant : 'X 99'") == "ligne de solde sans montant"
    assert script._kind("autre chose SECRET") == "autre"


def test_empty_folder_exits_one(tmp_path, capsys):
    assert script.main([str(tmp_path)]) == 1
    assert "Fichiers PDF : 0" in _out(capsys)


def test_library_logs_and_warnings_never_reach_output(tmp_path, capsys, monkeypatch):
    import logging
    import warnings
    folder = tmp_path / "rel"
    folder.mkdir()
    (folder / "A.PDF").write_bytes(b"%PDF-1.4 x")

    def noisy(*args, **kwargs):
        logging.getLogger("pdfminer").warning("SECRET 123")
        warnings.warn("SECRET 123")
        print("SECRET 123", file=__import__("sys").stderr)
        raise ValueError("SECRET")

    monkeypatch.setattr(script, "import_pdf", noisy)
    assert script.main([str(folder)]) == 1
    captured = capsys.readouterr()
    assert captured.err == "" and "SECRET" not in captured.out
    assert "Fichiers PDF : 1" in captured.out and "ValueError" in captured.out


def test_usage_error(capsys):
    assert script.main([]) == 2


CLASSIFY_LABELS = ("Classées par règle", "Classées par appariement", "Classées automatiquement (modèle)",
                   "Suggestions sous le seuil", "Sans suggestion", "Durée du dernier reclassement (ms)")


def test_classification_counters_are_integers_and_leak_nothing(tmp_path, capsys):
    folder = tmp_path / "rel"
    folder.mkdir()
    make_statement(folder / "a_secret.pdf", "10 avril 2024",
                   _good("11/03/2024", "10/04/2024", "PAIEMENT CB 0703 VILLEA"))
    assert script.main([str(folder)]) == 0
    out = _out(capsys)
    values = dict(line.split(" : ") for line in out.splitlines() if " : " in line)
    for label in CLASSIFY_LABELS:
        assert re.fullmatch(r"\d+", values[label]), label
    # 2 transactions non miroir : chaque ligne est classée ou suggérée ou sans suggestion
    total = sum(int(values[k]) for k in CLASSIFY_LABELS[:5])
    assert total == 2
    for forbidden in ("PAIEMENT", "MAGASIN", "EPARGNE", "secret", "00000000001"):
        assert forbidden not in out


def test_suggestion_counts_and_steady_state(tmp_path):
    from app.classify.pipeline import reclassify
    from app.store.db import connect
    conn = connect(tmp_path / "s.db")
    conn.execute("INSERT INTO accounts(id, number, label, kind) VALUES (1, '1', 'CC', 'checking')")
    savings = conn.execute("INSERT INTO categories(name, kind) VALUES ('Épargne', 'savings')").lastrowid
    food = conn.execute("INSERT INTO categories(name, kind) VALUES ('Courses', 'expense')").lastrowid
    rows = [("VIR VERS LIVRET FICTIF", -100, savings, 1), ("VIR VERS LIVRET FICTIF", -100, None, 0),
            ("CB MAGASIN ALPHA", -200, food, 1), ("CB MAGASIN ALPHA", -200, food, 1),
            ("CB MAGASIN ALPHA", -300, None, 0), ("XYZZY QWERTY", -50, None, 0), ("XYZZY QWERTY", -60, None, 0)]
    for i, (label, cents, cat, manual) in enumerate(rows):
        conn.execute("INSERT INTO transactions(account_id, tx_key, op_date, label, label_norm, amount_cents, "
                     "category_id, manual, classified_by) VALUES (1, ?, '2024-03-01', ?, ?, ?, ?, ?, ?)",
                     (f"k{i}", label, label, cents, cat, manual, "manual" if manual else None))
    reclassify(conn, threshold=1.0)
    assert script.suggestion_counts(conn) == {"sugg_savings": 1, "sugg_other": 1, "sugg_single": 1}
    assert script.steady_state_ms(conn) >= 0
    dummy = conn.execute("SELECT id FROM categories WHERE name = ?", (script.DUMMY_CATEGORY,)).fetchone()[0]
    marked = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id = ? AND classified_by = 'manual'",
                          (dummy,)).fetchone()[0]
    assert marked == 2          # 4 restantes (2 suggestions + 2 sans) : une sur deux
    conn.close()
