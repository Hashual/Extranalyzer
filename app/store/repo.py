import sqlite3


def account_kind_from_label(label: str) -> str:
    upper = label.upper()
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
    conn.commit()
    return cur.lastrowid
