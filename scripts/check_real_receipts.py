"""Vérification sur tickets réels : relit les PDF de `<data_dir>/receipts` dans une base jetable, n'affiche que des
compteurs.

Usage : python scripts/check_real_receipts.py <data_dir>
Aucun libellé, rayon, montant, date, magasin ni nom de fichier n'est affiché ; les messages d'erreur sont ramenés
à un type fixe (liste blanche).
"""
import contextlib
import io
import logging
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.receipts import PARSERS_RECEIPTS  # noqa: E402
from app.receipts.aisles import SEED, load_aisle_seed  # noqa: E402
from app.receipts.service import ingest_file  # noqa: E402
from app.store.db import connect  # noqa: E402

KINDS = ("écart de", "total introuvable", "date introuvable", "aucun article", "format de ticket non reconnu",
         "rendu sans paiement en espèces", "lecture impossible")
METHODS = ("card", "meal_voucher", "voucher", "cash", "other")


def _kind(message: str) -> str:
    """Type d'erreur parmi une liste fixe (jamais le texte du message, qui peut citer le contenu)."""
    return next((k for k in KINDS if message.startswith(k)), "autre")


def _scalar(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def collect(data_dir: Path) -> tuple[dict[str, int], Counter]:
    pdfs = sorted(p for p in (data_dir / "receipts").glob("*.pdf") if p.is_file())
    counts: Counter = Counter(pdf=len(pdfs))
    kinds: Counter = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "check.db")
        try:
            load_aisle_seed(conn, SEED)
            for pdf in pdfs:        # un PDF à la fois
                result = ingest_file(conn, pdf.read_bytes(), "ticket.pdf", Path(tmp) / "receipts",
                                     PARSERS_RECEIPTS, origin="upload")
                counts[result.outcome] += 1
                if result.status == "error":
                    kinds[_kind(result.message or "")] += 1
            conn.commit()
            for status in ("ok", "error"):
                counts[status] = conn.execute("SELECT COUNT(*) FROM receipts WHERE status=?", (status,)).fetchone()[0]
            counts["items"] = _scalar(conn, "SELECT COUNT(*) FROM receipt_lines WHERE kind='item'")
            counts["discounts"] = _scalar(conn, "SELECT COUNT(*) FROM receipt_lines WHERE kind='discount'")
            counts["aisles"] = _scalar(conn, "SELECT COUNT(DISTINCT aisle_id) FROM receipt_lines")
            counts["unsorted"] = _scalar(conn, "SELECT COUNT(DISTINCT l.aisle_id) FROM receipt_lines l "
                                               "JOIN aisles a ON a.id = l.aisle_id WHERE a.family_id IS NULL")
            counts["no_aisle_lines"] = _scalar(conn, "SELECT COUNT(*) FROM receipt_lines l JOIN aisles a "
                                                     "ON a.id = l.aisle_id WHERE a.name = 'SANS RAYON'")
            for method in METHODS:
                counts[f"tickets_{method}"] = conn.execute(
                    "SELECT COUNT(DISTINCT receipt_id) FROM receipt_payments WHERE method=?", (method,)).fetchone()[0]
            counts["no_card"] = _scalar(conn, "SELECT COUNT(*) FROM receipts r WHERE r.status='ok' AND NOT EXISTS "
                                              "(SELECT 1 FROM receipt_payments p WHERE p.receipt_id = r.id "
                                              "AND p.method='card')")
        finally:
            conn.close()
    return counts, kinds


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not (Path(args[0]) / "receipts").is_dir():
        print("Usage : python scripts/check_real_receipts.py <data_dir>   (contenant receipts/)")
        return 2
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
            warnings.simplefilter("ignore")
            counts, kinds = collect(Path(args[0]))
    finally:
        logging.disable(logging.NOTSET)
    lines = [
        ("Fichiers PDF", counts["pdf"]),
        ("Tickets de caisse", counts["stored"]),
        ("Tickets CB", counts["slip"]),
        ("Fichiers en double", counts["duplicate"]),
        ("Fichiers refusés", counts["rejected"]),
        ("Tickets ok", counts["ok"]),
        ("Tickets error", counts["error"]),
        ("Lignes d'article", counts["items"]),
        ("Lignes de remise", counts["discounts"]),
        ("Lignes sans rayon imprimé", counts["no_aisle_lines"]),
        ("Rayons utilisés", counts["aisles"]),
        ("Rayons sans famille", counts["unsorted"]),
        ("Tickets payés par carte bancaire", counts["tickets_card"]),
        ("Tickets avec titre-restaurant", counts["tickets_meal_voucher"]),
        ("Tickets avec bon ou cagnotte", counts["tickets_voucher"]),
        ("Tickets avec espèces", counts["tickets_cash"]),
        ("Tickets avec paiement non classé", counts["tickets_other"]),
        ("Tickets ok sans carte bancaire", counts["no_card"]),
    ]
    for label, value in lines:
        print(f"{label} : {value}")
    for kind, n in sorted(kinds.items()):
        print(f"  type d'erreur : {kind} x{n}")
    clean = counts["error"] == counts["rejected"] == counts["unsorted"] == counts["tickets_other"] == 0
    return 0 if clean and counts["stored"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
