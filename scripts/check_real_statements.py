"""Vérification sur relevés réels : importe un dossier de PDF dans une base jetable, n'affiche que des compteurs.

Usage : python scripts/check_real_statements.py <dossier>
Aucun libellé, montant, date, nom de fichier ni numéro de compte n'est affiché ; les messages d'erreur
sont ramenés à un type fixe (liste blanche).
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

from app.classify import pipeline  # noqa: E402
from app.classify.seed import load_seed  # noqa: E402
from app.importer.creditmutuel import CreditMutuelParser  # noqa: E402
from app.importer.service import import_pdf  # noqa: E402
from app.store.db import connect  # noqa: E402

SEED = ROOT / "app" / "classify" / "rules.seed.toml"
STATUSES = ("ok", "error", "superseded", "conflict")
KINDS = ("ligne de solde sans montant", "date du relevé introuvable", "PDF illisible",
         "soldes introuvables", "transaction sans montant", "en-tête de compte/colonnes introuvables",
         "aucune section")


def _kind(message: str) -> str:
    """Type d'erreur parmi une liste fixe (jamais le texte du message, qui peut citer le contenu)."""
    return next((k for k in KINDS if message.startswith(k)), "autre")


def _scalar(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def collect(folder: Path) -> tuple[dict[str, int], Counter]:
    pdfs = sorted(p for p in folder.glob("*.pdf") if p.is_file() and p.suffix.lower() == ".pdf")
    counts = {"pdf": len(pdfs), "duplicates": 0, "import_errors": 0, "info_lines": 0}
    kinds: Counter = Counter()
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "check.db")
        try:
            load_seed(conn, SEED)
            for pdf in pdfs:
                try:
                    report = import_pdf(conn, pdf.read_bytes(), pdf.name, Path(tmp) / "pdfs",
                                        [CreditMutuelParser()], pipeline.on_new_transactions)
                except Exception as exc:  # noqa: BLE001 - on ne garde que le type
                    counts["import_errors"] += 1
                    kinds[type(exc).__name__] += 1
                    continue
                if report.duplicate_file:
                    counts["duplicates"] += 1
                elif report.error:
                    counts["import_errors"] += 1
                    kinds[_kind(report.error)] += 1
                counts["info_lines"] += sum(s.info_lines for s in report.sections)
            counts["documents"] = _scalar(conn, "SELECT COUNT(*) FROM documents")
            for status in STATUSES:
                counts[status] = conn.execute("SELECT COUNT(*) FROM sections WHERE status = ?",
                                              (status,)).fetchone()[0]
            counts["transactions"] = _scalar(conn, "SELECT COUNT(*) FROM transactions")
            counts["unclassified"] = _scalar(
                conn, "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND mirror_of IS NULL")
            counts["transfer_pairs"] = _scalar(
                conn, "SELECT COUNT(*) FROM transactions WHERE mirror_of IS NOT NULL")
        finally:
            conn.close()
    return counts, kinds


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not Path(args[0]).is_dir():
        print("Usage : python scripts/check_real_statements.py <dossier>")
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
        ("Fichiers en double", counts["duplicates"]),
        ("Documents importés", counts["documents"]),
        ("Sections ok", counts["ok"]),
        ("Sections error", counts["error"]),
        ("Sections superseded", counts["superseded"]),
        ("Sections conflict", counts["conflict"]),
        ("Transactions", counts["transactions"]),
        ("Transactions à classer", counts["unclassified"]),
        ("Paires de virements internes", counts["transfer_pairs"]),
        ("Lignes d'information", counts["info_lines"]),
        ("Erreurs d'import", counts["import_errors"]),
    ]
    for label, value in lines:
        print(f"{label} : {value}")
    for kind, n in sorted(kinds.items()):
        print(f"  type d'erreur : {kind} x{n}")
    clean = counts["error"] == counts["conflict"] == counts["import_errors"] == 0
    return 0 if clean and counts["pdf"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
