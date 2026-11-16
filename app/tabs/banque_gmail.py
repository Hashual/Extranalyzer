"""Tickets de caisse (Gmail) : section de l'onglet Connexions (connexion, état et relève de la boîte Gmail).

Le mot de passe d'application n'est jamais renvoyé : seule l'adresse, partiellement masquée, est exposée.
« Relever maintenant » passe par `POST /gmail/sync` (même passage que `POST /api/tabs/courses/sync`).
"""
import json
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.deps import get_conn
from app.receipts.mailruntime import (MailCredentialsInvalid, MailManagedByServer, MailNotConfigured,
                                      MailRuntime, MailUnreachable)

router = APIRouter()


MAX_BODY_BYTES = 4096


async def _credentials(request: Request) -> tuple[str, str]:
    """Corps `{address, app_password}` lu à la main : une erreur de validation automatique de FastAPI renverrait
    la saisie (donc le mot de passe) dans sa réponse. Taille bornée avant et pendant la lecture."""
    try:
        length = int(request.headers["content-length"])
    except (KeyError, ValueError):
        raise HTTPException(411, "en-tête Content-Length requis") from None
    if length > MAX_BODY_BYTES:
        raise HTTPException(413, "requête trop volumineuse")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "requête trop volumineuse")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    address = body.get("address") if isinstance(body, dict) else None
    password = body.get("app_password") if isinstance(body, dict) else None
    if not isinstance(address, str) or not isinstance(password, str):
        raise HTTPException(422, "renseignez l'adresse Gmail et le mot de passe d'application")
    return address, password


def _runtime(request: Request) -> MailRuntime:
    return request.app.state.mail


def _status(runtime: MailRuntime, conn: sqlite3.Connection) -> dict:
    rows = {label: (success, attempt, error) for label, success, attempt, error in conn.execute(
        "SELECT label, last_success_at, last_attempt_at, last_error FROM mail_sync")}
    labels = []
    for label, store in runtime.labels:
        success, attempt, error = rows.get(label, (None, None, None))
        labels.append({"label": label, "store": store, "last_success_at": success, "last_attempt_at": attempt,
                       "last_error": error})
    return {"source": runtime.source, "configured": runtime.enabled, "address": runtime.address,
            "labels": labels}


@router.get("")
def gmail_status(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    return _status(_runtime(request), conn)


@router.put("/credentials")
async def set_credentials(request: Request):
    """Teste la connexion IMAP (lecture seule) puis enregistre les identifiants ; la relève démarre aussitôt."""
    address, password = await _credentials(request)
    try:
        await run_in_threadpool(_runtime(request).connect, address, password)
    except MailManagedByServer as exc:
        raise HTTPException(409, str(exc)) from None
    except MailCredentialsInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    except MailUnreachable as exc:
        raise HTTPException(502, str(exc)) from None
    return {"ok": True, "address": _runtime(request).address}


@router.delete("/credentials")
def delete_credentials(request: Request):
    try:
        _runtime(request).disconnect()
    except MailManagedByServer as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@router.post("/sync")
async def sync_now(request: Request):
    try:
        return await run_in_threadpool(_runtime(request).run_once)
    except MailNotConfigured as exc:
        raise HTTPException(409, str(exc)) from None
