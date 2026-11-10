import sqlite3
import unicodedata


def account_kind_from_label(label: str) -> str:
    upper = unicodedata.normalize("NFD", label).encode("ascii", "ignore").decode().upper()
    if "LIVRET" in upper or "EPARGNE" in upper:
        return "savings"
    return "checking"


def upsert_account(conn: sqlite3.Connection, number: str, label: str) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE number = ?", (number,)).fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO accounts(number, label, kind) VALUES (?, ?, ?)",
        (number, label, account_kind_from_label(label)),
    )
    return cur.lastrowid


# --- Documents, sections, transactions (aucune fonction ne valide : l'appelant gère la transaction) ---

def document_id_by_sha(conn: sqlite3.Connection, sha256: str) -> int | None:
    row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    return None if row is None else row[0]


def insert_document(conn: sqlite3.Connection, sha256: str, filename: str, stored_path: str, parser: str,
                    statement_date: str, imported_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents(sha256, filename, stored_path, parser, statement_date, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha256, filename, stored_path, parser, statement_date, imported_at),
    )
    return cur.lastrowid


def list_documents(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(id, stored_path) de chaque document, dans l'ordre d'import."""
    return conn.execute("SELECT id, stored_path FROM documents ORDER BY id").fetchall()


def set_document_parser(conn: sqlite3.Connection, document_id: int, parser: str) -> None:
    conn.execute("UPDATE documents SET parser = ? WHERE id = ?", (parser, document_id))


def insert_section(conn: sqlite3.Connection, document_id: int, account_id: int, statement_date: str,
                   opening_cents: int, closing_cents: int, gap_cents: int, status: str,
                   content_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO sections(document_id, account_id, statement_date, opening_cents, closing_cents, "
        "gap_cents, status, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, account_id, statement_date, opening_cents, closing_cents, gap_cents, status,
         content_hash),
    )
    return cur.lastrowid


def update_section(conn: sqlite3.Connection, section_id: int, opening_cents: int, closing_cents: int,
                   gap_cents: int, status: str, content_hash: str) -> None:
    conn.execute(
        "UPDATE sections SET opening_cents = ?, closing_cents = ?, gap_cents = ?, status = ?, "
        "content_hash = ? WHERE id = ?",
        (opening_cents, closing_cents, gap_cents, status, content_hash, section_id),
    )


def set_section_status(conn: sqlite3.Connection, section_id: int, status: str) -> None:
    conn.execute("UPDATE sections SET status = ? WHERE id = ?", (status, section_id))


def get_section(conn: sqlite3.Connection, section_id: int) -> tuple | None:
    """(document_id, account_id, account_number, statement_date, status, stored_path) ou None."""
    return conn.execute(
        "SELECT s.document_id, s.account_id, a.number, s.statement_date, s.status, d.stored_path "
        "FROM sections s JOIN accounts a ON a.id = s.account_id JOIN documents d ON d.id = s.document_id "
        "WHERE s.id = ?",
        (section_id,),
    ).fetchone()


def find_ok_section(conn: sqlite3.Connection, account_id: int, statement_date: str,
                    exclude_id: int | None = None) -> tuple[int, str] | None:
    """(id, content_hash) de la section `ok` du même compte et de la même date, s'il y en a une."""
    return conn.execute(
        "SELECT id, content_hash FROM sections WHERE account_id = ? AND statement_date = ? "
        "AND status = 'ok' AND id IS NOT ? ORDER BY id LIMIT 1",
        (account_id, statement_date, exclude_id),
    ).fetchone()


def find_document_section(conn: sqlite3.Connection, document_id: int, account_id: int,
                          statement_date: str) -> tuple[int, str] | None:
    """(id, status) de la section de ce document pour ce compte et cette date."""
    return conn.execute(
        "SELECT id, status FROM sections WHERE document_id = ? AND account_id = ? AND statement_date = ? "
        "ORDER BY id LIMIT 1",
        (document_id, account_id, statement_date),
    ).fetchone()


def delete_section_transactions(conn: sqlite3.Connection, section_id: int) -> None:
    """Supprime les transactions de la section (les liens de virement interne vers elles sont levés)."""
    conn.execute(
        "UPDATE transactions SET mirror_of = NULL "
        "WHERE mirror_of IN (SELECT id FROM transactions WHERE section_id = ?)",
        (section_id,),
    )
    conn.execute("DELETE FROM transactions WHERE section_id = ?", (section_id,))


def find_key_collision(conn: sqlite3.Connection, tx_keys: list[str],
                       exclude_section_id: int | None = None) -> tuple[str, str | None] | None:
    """(numéro de compte, date du relevé) de la première transaction existante portant l'une des
    `tx_keys`, hors section `exclude_section_id` ; None s'il n'y en a pas."""
    for key in tx_keys:
        row = conn.execute(
            "SELECT a.number, s.statement_date FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "LEFT JOIN sections s ON s.id = t.section_id "
            "WHERE t.tx_key = ? AND COALESCE(t.section_id, -1) != COALESCE(?, -2)",
            (key, exclude_section_id),
        ).fetchone()
        if row is not None:
            return row
    return None


def list_document_sections(conn: sqlite3.Connection, document_id: int) -> list[tuple]:
    """(id, numéro de compte, statement_date, status, gap_cents) des sections du document."""
    return conn.execute(
        "SELECT s.id, a.number, s.statement_date, s.status, s.gap_cents FROM sections s "
        "JOIN accounts a ON a.id = s.account_id WHERE s.document_id = ? ORDER BY s.id",
        (document_id,),
    ).fetchall()


def insert_transaction(conn: sqlite3.Connection, section_id: int, account_id: int, tx_key: str,
                       op_date: str, value_date: str, label: str, label_norm: str,
                       amount_cents: int) -> int:
    """Insère une transaction ; un reclassement manuel existant pour `tx_key` est ré-appliqué."""
    row = conn.execute("SELECT category_id FROM manual_overrides WHERE tx_key = ?", (tx_key,)).fetchone()
    category_id, manual = (None, 0) if row is None else (row[0], 1)
    cur = conn.execute(
        "INSERT INTO transactions(section_id, account_id, tx_key, op_date, value_date, label, label_norm, "
        "amount_cents, category_id, manual) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (section_id, account_id, tx_key, op_date, value_date, label, label_norm, amount_cents,
         category_id, manual),
    )
    return cur.lastrowid
