"""Chaînage du classement après import : règles, appariement des virements, synchronisation des miroirs."""
import sqlite3

from app.classify.rules import apply_rules, sync_mirrors
from app.classify.transfers import pair_internal_transfers


def on_new_transactions(conn: sqlite3.Connection, new_ids: list[int]) -> None:
    apply_rules(conn, new_ids)
    pair_internal_transfers(conn)
    sync_mirrors(conn)
