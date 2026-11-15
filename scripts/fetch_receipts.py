"""Relève réelle des tickets de caisse dans une base locale ; n'affiche que des compteurs.

Usage : python scripts/fetch_receipts.py <dossier de données>
Variables : EXTRANALYZER_GMAIL_USER et EXTRANALYZER_GMAIL_APP_PASSWORD(_FILE), voir app.config.
Gmail est ouvert en lecture seule. Ni adresse, ni mot de passe, ni contenu de ticket n'est affiché.
"""
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import mail_settings  # noqa: E402
from app.receipts.mailsync import sync_once  # noqa: E402
from app.store.db import connect  # noqa: E402

T = TypeVar("T")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage : fetch_receipts.py <dossier de données>")
        return 2
    cfg = mail_settings()
    if cfg is None:
        print("relève désactivée : définir EXTRANALYZER_GMAIL_USER et le mot de passe d'application")
        return 2
    data_dir = Path(argv[1])
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "extranalyzer.db"

    def write(fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = connect(db_path)
        try:
            return fn(conn)
        finally:
            conn.close()

    result = sync_once(cfg, write, data_dir / "receipts", [])
    print(f"messages: {result['messages']}")
    print(f"files: {result['files']}")
    print(f"error: {result['error']}")
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
