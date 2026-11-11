"""Dépendances FastAPI partagées par l'application et les onglets (sans importer `app.main`)."""
import sqlite3
from collections.abc import Iterator

from fastapi import Request

from app.store.db import connect


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Dépendance : une connexion par requête (ouverte/fermée par FastAPI, éventuellement dans
    des threads différents de celui de la route, d'où `check_same_thread=False`)."""
    conn = connect(request.app.state.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()
