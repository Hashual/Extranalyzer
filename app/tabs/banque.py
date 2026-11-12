"""Onglet « Banque » : connexion Enable Banking, comptes bancaires et leur rattachement, synchros.

Le runtime (`request.app.state.bank`, un `BankRuntime`) porte le client et le verrou d'écriture. Aucun IBAN complet,
jeton ni identifiant de session n'est exposé : seulement les 4 derniers caractères de l'IBAN.
"""
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.deps import get_conn
from app.importer.amounts import from_cents
from app.sources.enablebanking.client import EnableBankingError
from app.sources.enablebanking.link import StateError, accept_gap, attach, bank_status
from app.sources.enablebanking.runtime import BankRuntime, NotConfigured, SyncBusy
from app.tabs.registry import Tab
from app.web_json import mask_numbers, to_jsonable

router = APIRouter()


class SessionBody(BaseModel):
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None
    url: str | None = None          # repli : adresse de retour collée par l'utilisateur


class AttachBody(BaseModel):
    account_id: int | None = None   # None : nouveau compte
    ignore: bool = False


def _runtime(request: Request) -> BankRuntime:
    return request.app.state.bank


def _call(fn):
    """Exécute une action et traduit ses erreurs en réponses HTTP (messages masqués des numéros)."""
    try:
        return fn()
    except NotConfigured as exc:
        raise HTTPException(409, str(exc)) from None
    except SyncBusy as exc:
        raise HTTPException(409, str(exc)) from None
    except StateError as exc:
        raise HTTPException(400, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, mask_numbers(str(exc))) from None
    except ValueError as exc:
        raise HTTPException(409, mask_numbers(str(exc))) from None
    except EnableBankingError as exc:
        raise HTTPException(502, f"Enable Banking : {mask_numbers(exc.message)}") from None


def _summary(s) -> dict:
    return {"added": s.added, "skipped": s.skipped, "errors": s.errors,
            "message": None if s.message is None else mask_numbers(s.message)}


def _tail(value: str | None) -> str:
    return (value or "")[-4:]


@router.get("/status")
def status(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    runtime = _runtime(request)
    bank = runtime.bank
    link = conn.execute("SELECT id, aspsp_name, status, valid_until, created_at, last_error, retry_after "
                        "FROM bank_links WHERE status IN ('active', 'expired') ORDER BY id DESC LIMIT 1").fetchone()
    accounts = []
    if link is not None:
        for (ext_id, name, iban, kind, account_id, ignored, last_sync, balance, balance_date, gap, accepted,
             error, label, number) in conn.execute(
                "SELECT e.id, e.name, e.iban, e.cash_account_type, e.account_id, e.ignored, e.last_sync_at, "
                "e.balance_cents, e.balance_date, e.balance_gap_cents, e.accepted_gap_cents, e.last_error, a.label, "
                "a.number FROM external_accounts e LEFT JOIN accounts a ON a.id = e.account_id "
                "WHERE e.link_id = ? ORDER BY e.id", (link[0],)):
            imported = account_id is not None and conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = ? AND source = 'enablebanking' LIMIT 1",
                (account_id,)).fetchone() is not None
            accounts.append({
                "id": ext_id, "name": mask_numbers(name or "Compte"), "iban_tail": _tail(iban), "kind": kind,
                "account_id": account_id, "account_label": None if label is None else mask_numbers(label),
                "account_tail": _tail(number), "ignored": bool(ignored), "locked": imported, "last_sync": last_sync,
                "balance": None if balance is None else from_cents(balance), "balance_date": balance_date,
                "gap": None if gap is None else from_cents(gap),
                "accepted_gap": from_cents(accepted), "error": None if error is None else mask_numbers(error)})
    runs = [{"started_at": started, "finished_at": finished, "trigger": trigger, "added": added, "skipped": skipped,
             "errors": errors, "message": None if message is None else mask_numbers(message)}
            for started, finished, trigger, added, skipped, errors, message in conn.execute(
                "SELECT started_at, finished_at, trigger, added, skipped, errors, message FROM sync_runs "
                "ORDER BY id DESC LIMIT 10")]
    known = [{"id": i, "label": mask_numbers(label), "number_tail": number[-4:]}
             for i, label, number in conn.execute("SELECT id, label, number FROM accounts ORDER BY id")]
    return to_jsonable({
        "configured": runtime.configured,
        "aspsp_name": None if bank is None else bank.aspsp_name,
        "redirect_url": None if bank is None else bank.redirect_url,
        "link": None if link is None else {
            "aspsp_name": link[1], "status": link[2], "valid_until": link[3], "created_at": link[4],
            "error": None if link[5] is None else mask_numbers(link[5]), "retry_after": link[6]},
        "alert": bank_status(conn, datetime.now(timezone.utc)),
        "accounts": accounts, "runs": runs, "known_accounts": known})


@router.post("/connect")
def connect_bank(request: Request):
    return {"url": _call(_runtime(request).start)}


def _from_pasted(url: str) -> SessionBody:
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url.strip()).query))
    return SessionBody(code=query.get("code"), state=query.get("state"), error=query.get("error"),
                       error_description=query.get("error_description"))


@router.post("/session")
def finish_connection(body: SessionBody, request: Request):
    """Retour de la banque (page /bank/callback ou adresse collée) : `code` et `state`, ou `error`."""
    if body.url:
        body = _from_pasted(body.url)
    if not body.state:
        raise HTTPException(400, "adresse de retour incomplète : paramètre state absent")
    runtime = _runtime(request)
    if body.error or not body.code:
        _call(lambda: runtime.reject(body.state))
        reason = body.error_description or body.error or "code d'autorisation absent"
        raise HTTPException(400, f"autorisation refusée par la banque : {mask_numbers(reason[:200])}")
    return _summary(_call(lambda: runtime.finish(body.code, body.state)))


@router.post("/sync")
def sync_now(request: Request):
    return _summary(_call(lambda: _runtime(request).sync("manuelle")))


@router.post("/accounts/{external_id}/attach")
def attach_account(external_id: int, body: AttachBody, request: Request):
    runtime = _runtime(request)
    _call(lambda: runtime.write(lambda conn: _commit(conn, attach, external_id, body.account_id,
                                                      ignore=body.ignore)))
    return {"ok": True}


@router.post("/accounts/{external_id}/accept-gap")
def accept_account_gap(external_id: int, request: Request):
    _call(lambda: _runtime(request).write(lambda conn: _commit(conn, accept_gap, external_id)))
    return {"ok": True}


@router.post("/disconnect")
def disconnect(request: Request):
    _call(_runtime(request).disconnect)
    return {"ok": True}


def _commit(conn: sqlite3.Connection, fn, *args, **kwargs):
    try:
        result = fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


TAB = Tab("banque", "Banque", 65, router)
