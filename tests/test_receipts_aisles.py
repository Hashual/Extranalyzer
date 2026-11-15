import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from app.receipts.aisles import SEED, aisle_id, load_aisle_seed, normalize_aisle
from app.receipts.model import ParsedReceipt, Payment, ReceiptLine
from app.receipts.service import ingest_file
from app.store.db import connect

FAMILIES = ["Crèmerie", "Fruits & légumes", "Viandes & poissons", "Épicerie salée", "Épicerie sucrée",
            "Boissons", "Surgelés", "Boulangerie & pâtisserie", "Hygiène & beauté", "Entretien",
            "Maison & divers", "Animaux", "Remises"]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def write_seed(path: Path, aisles: dict[str, str]) -> Path:
    body = '[[family]]\nname = "Crèmerie"\n[[family]]\nname = "Remises"\n[aisles.superu]\n'
    body += "".join(f'"{k}" = "{v}"\n' for k, v in aisles.items())
    path.write_text(body, encoding="utf-8")
    return path


def family_of(conn, name: str) -> str | None:
    row = conn.execute("SELECT f.name FROM aisles a LEFT JOIN aisle_families f ON f.id = a.family_id "
                       "WHERE a.store='superu' AND a.name=?", (name,)).fetchone()
    return row[0]


def test_normalize_aisle():
    assert normalize_aisle("  Fromage   LS ") == "FROMAGE LS"
    assert normalize_aisle("Crèmerie l.s.") == "CREMERIE L.S."


def test_unknown_aisle_is_created_without_family(conn):
    first = aisle_id(conn, "superu", "Rayon  Inconnu")
    assert aisle_id(conn, "superu", "RAYON INCONNU") == first
    assert aisle_id(conn, "autre", "RAYON INCONNU") != first        # rayons propres à chaque enseigne
    assert family_of(conn, "RAYON INCONNU") is None


def test_seed_never_overwrites_user_family(conn, tmp_path):
    seed = write_seed(tmp_path / "s.toml", {"BEURRE": "Crèmerie", "LOT TEST": "Crèmerie"})
    load_aisle_seed(conn, seed)
    remises = conn.execute("SELECT id FROM aisle_families WHERE name='Remises'").fetchone()[0]
    conn.execute("UPDATE aisles SET family_id=? WHERE name='LOT TEST'", (remises,))   # choix de l'utilisateur
    load_aisle_seed(conn, seed)
    assert family_of(conn, "LOT TEST") == "Remises" and family_of(conn, "BEURRE") == "Crèmerie"
    assert conn.execute("SELECT name, position FROM aisle_families ORDER BY position").fetchall() == [
        ("Crèmerie", 0), ("Remises", 1)]
    assert conn.execute("SELECT COUNT(*) FROM aisles").fetchone()[0] == 2


def test_seed_with_unknown_family_is_refused(conn, tmp_path):
    with pytest.raises(ValueError, match="famille inconnue"):
        load_aisle_seed(conn, write_seed(tmp_path / "s.toml", {"BEURRE": "Famille absente"}))


def test_shipped_seed_lists_spec_families_and_files_every_aisle(conn):
    load_aisle_seed(conn, SEED)
    assert [r[0] for r in conn.execute("SELECT name FROM aisle_families ORDER BY position")] == FAMILIES
    data = tomllib.loads(SEED.read_text(encoding="utf-8"))
    assert all(normalize_aisle(k) == k for k in data["aisles"]["superu"])
    assert conn.execute("SELECT COUNT(*) FROM aisles WHERE family_id IS NULL").fetchone()[0] == 0
    assert family_of(conn, "REMISES") == "Remises"
    # articles sans rayon imprimé : à ranger, jamais dans une famille par défaut (spec § 3)
    aisle_id(conn, "superu", "SANS RAYON")
    assert family_of(conn, "SANS RAYON") is None


class OneLineParser:
    store = "superu"

    def kind(self, path):
        return "receipt"

    def parse(self, path):
        return ParsedReceipt("MAGASIN TEST", datetime(2025, 1, 1, 10, 0), 300,
                             [ReceiptLine("  ultra   frais ", "YAOURT FICTIF", 1, 300, 300, "item")],
                             [Payment("card", 300)])


def test_lines_get_their_store_aisle(conn, tmp_path):
    load_aisle_seed(conn, SEED)
    ingest_file(conn, b"%PDF-1.4 x", "t.pdf", tmp_path, [OneLineParser()], origin="upload")
    assert conn.execute("SELECT a.store, a.name, f.name FROM receipt_lines l JOIN aisles a ON a.id = l.aisle_id "
                        "LEFT JOIN aisle_families f ON f.id = a.family_id").fetchall() == [
        ("superu", "ULTRA FRAIS", "Crèmerie")]
