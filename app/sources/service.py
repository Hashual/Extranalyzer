"""Écriture générique des opérations d'une source externe : frontière avec les relevés PDF, identité stable,
insertion sans doublon, contrôle de solde.

Ne valide pas : l'appelant possède la transaction.
"""
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date

from app.classify.normalize import normalize_label
from app.importer.amounts import to_cents
from app.sources.base import AccountFetch, FetchedBalance
from app.store import repo


def frontier(conn: sqlite3.Connection, account_id: int) -> date | None:
    """Date du dernier relevé PDF `ok` du compte : seules les opérations postérieures viennent d'une source."""
    row = conn.execute("SELECT MAX(statement_date) FROM sections WHERE account_id = ? AND status = 'ok'",
                       (account_id,)).fetchone()
    return None if row[0] is None else date.fromisoformat(row[0])


def source_tx_key(source: str, account_number: str, op_date: date, amount_cents: int, label_norm: str,
                  rank: int) -> str:
    raw = f"{source}|{account_number}|{op_date.isoformat()}|{amount_cents}|{label_norm}|{rank}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def store_fetch(conn: sqlite3.Connection, source: str, fetch: AccountFetch) -> tuple[list[int], int]:
    """Insère les opérations postérieures à la frontière qui ne sont pas déjà en base.

    Renvoie (ids insérés, nombre d'opérations ignorées car antérieures ou égales à la frontière). Le rang d'une
    opération compte ses doublons exacts (date, montant, libellé normalisé) dans la réponse : une source livre
    toujours des journées entières, le rang ne dépend donc pas de l'ordre de la réponse.
    """
    number = conn.execute("SELECT number FROM accounts WHERE id = ?", (fetch.account_id,)).fetchone()[0]
    limit = frontier(conn, fetch.account_id)
    ranks: Counter = Counter()
    inserted, skipped = [], 0
    for tx in sorted(fetch.transactions, key=lambda t: t.op_date):
        if limit is not None and tx.op_date <= limit:
            skipped += 1
            continue
        norm, cents = normalize_label(tx.label), to_cents(tx.amount)
        identity = (tx.op_date, cents, norm)
        key = source_tx_key(source, number, tx.op_date, cents, norm, ranks[identity])
        ranks[identity] += 1
        if conn.execute("SELECT 1 FROM transactions WHERE tx_key = ?", (key,)).fetchone() is not None:
            continue
        tx_id = repo.insert_transaction(conn, None, fetch.account_id, key, tx.op_date.isoformat(),
                                        None if tx.value_date is None else tx.value_date.isoformat(),
                                        tx.label, norm, cents, source=source)
        conn.execute("INSERT INTO external_transactions(transaction_id, raw_json) VALUES (?, ?)",
                     (tx_id, json.dumps(tx.raw, ensure_ascii=False, sort_keys=True)))
        inserted.append(tx_id)
    return inserted, skipped


def balance_gap(conn: sqlite3.Connection, account_id: int, balance: FetchedBalance | None) -> int | None:
    """Écart en centimes entre le solde annoncé par la source et le solde attendu (clôture du dernier relevé PDF
    + opérations externes jusqu'à la date du solde) ; None si le contrôle est impossible."""
    if balance is None:
        return None
    row = conn.execute("SELECT closing_cents, statement_date FROM sections WHERE account_id = ? AND status = 'ok' "
                       "AND closing_cents IS NOT NULL ORDER BY statement_date DESC, id DESC LIMIT 1",
                       (account_id,)).fetchone()
    if row is None or balance.reference_date.isoformat() < row[1]:
        return None
    closing, statement_date = row
    moved = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id = ? "
                         "AND source != 'pdf' AND op_date > ? AND op_date <= ?",
                         (account_id, statement_date, balance.reference_date.isoformat())).fetchone()[0]
    return to_cents(balance.amount) - (closing + moved)
