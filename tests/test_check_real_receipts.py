import importlib.util
import re
from pathlib import Path

from receiptgen import Item, make_superu_receipt, make_superu_slip

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_real_receipts.py"
spec = importlib.util.spec_from_file_location("check_real_receipts", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def test_counters_only(tmp_path, capsys):
    folder = tmp_path / "data" / "receipts"
    folder.mkdir(parents=True)
    make_superu_receipt(folder / "a_secret.pdf", aisles=[("RAYON FICTIF", [Item("ARTICLE SECRET", 1234)])],
                        payments=[("CB", 734), ("TITRE RESTAURANT", 500)])
    make_superu_receipt(folder / "b.pdf", aisles=[("FRUITS", [Item("ARTICLE SECRET", 250)])],
                        total_cents=999, payments=[("CB", 999)])
    make_superu_slip(folder / "c.pdf", amount_cents=734)
    (folder / "d.pdf").write_bytes(b"%PDF-1.4 inconnu")

    code = script.main([str(tmp_path / "data")])
    out = capsys.readouterr().out
    values = dict(line.split(" : ") for line in out.splitlines() if " : " in line)
    assert code == 1
    assert (values["Fichiers PDF"], values["Tickets de caisse"], values["Tickets CB"]) == ("4", "2", "1")
    assert (values["Fichiers refusés"], values["Tickets ok"], values["Tickets error"]) == ("1", "1", "1")
    assert values["Rayons sans famille (hors SANS RAYON)"] == "1"                 # RAYON FICTIF n'est pas dans le seed
    assert values["Tickets avec titre-restaurant"] == "1"
    assert "type d'erreur : écart de x1" in out
    for forbidden in ("SECRET", "FICTIF", "MAGASIN", "secret", "12,34", "7,34", "2025"):
        assert forbidden not in out
    assert all(len(d) < 3 for d in re.findall(r"\d+", out))


def test_tickets_without_aisle_are_counted_apart_and_stay_clean(tmp_path, capsys):
    folder = tmp_path / "data" / "receipts"
    folder.mkdir(parents=True)
    make_superu_receipt(folder / "a.pdf", layout="classic", aisles=[(None, [Item("ARTICLE A", 120), Item("B", 80)])])
    assert script.main([str(tmp_path / "data")]) == 0
    values = dict(line.split(" : ") for line in capsys.readouterr().out.splitlines() if " : " in line)
    assert values["Lignes sans rayon imprimé (à ranger)"] == "2"
    assert values["Rayons sans famille (hors SANS RAYON)"] == "0"


def test_usage_without_receipts_folder(tmp_path, capsys):
    assert script.main([str(tmp_path)]) == 2
