"""Onglet « Connexions » (id `banque`) : connexion Enable Banking, comptes bancaires et leur rattachement, synchros.

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
from app.sources.enablebanking.link import StateError, accept_gap, attach, bank_status, link_state, set_kind
from app.sources.enablebanking.runtime import BankRuntime, NotConfigured, SyncBusy
from app.tabs import banque_gmail, banque_wallets
from app.tabs.registry import Tab
from app.web_json import mask_numbers, to_jsonable

router = APIRouter()
router.include_router(banque_wallets.router, prefix="/wallets")
router.include_router(banque_gmail.router, prefix="/gmail")


class SessionBody(BaseModel):
    code: str | None = None
    state: str | None = None
    error: str | None = None
    error_description: str | None = None
    url: str | None = None          # repli : adresse de retour collée par l'utilisateur


class ConnectBody(BaseModel):
    aspsp_name: str | None = None   # None : banque de la configuration (EXTRANALYZER_EB_ASPSP)
    country: str | None = None


class SyncBody(BaseModel):
    link_id: int | None = None      # None : toutes les banques connectées


class KindBody(BaseModel):
    kind: str


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


def _accounts(conn: sqlite3.Connection, link_id: int) -> list[dict]:
    accounts = []
    for (ext_id, name, iban, kind, account_id, ignored, last_sync, balance, balance_date, gap, accepted,
         error, label, number, account_kind, account_source) in conn.execute(
            "SELECT e.id, e.name, e.iban, e.cash_account_type, e.account_id, e.ignored, e.last_sync_at, "
            "e.balance_cents, e.balance_date, e.balance_gap_cents, e.accepted_gap_cents, e.last_error, a.label, "
            "a.number, a.kind, a.source FROM external_accounts e LEFT JOIN accounts a ON a.id = e.account_id "
            "WHERE e.link_id = ? ORDER BY e.id", (link_id,)):
        imported = account_id is not None and conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND source = 'enablebanking' LIMIT 1",
            (account_id,)).fetchone() is not None
        accounts.append({
            "id": ext_id, "name": mask_numbers(name or "Compte"), "iban_tail": _tail(iban), "kind": kind,
            "account_id": account_id, "account_label": None if label is None else mask_numbers(label),
            "account_tail": _tail(number), "account_kind": account_kind,
            "api_only": account_source == "enablebanking", "ignored": bool(ignored), "locked": imported,
            "last_sync": last_sync, "balance": None if balance is None else from_cents(balance),
            "balance_date": balance_date, "gap": None if gap is None else from_cents(gap),
            "accepted_gap": from_cents(accepted), "error": None if error is None else mask_numbers(error)})
    return accounts


@router.get("/status")
def status(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    runtime = _runtime(request)
    bank = runtime.bank
    now = datetime.now(timezone.utc)
    links = [{"id": link_id, "aspsp_name": name, "country": country, "status": link_status,
              "valid_until": valid_until, "created_at": created_at,
              "error": None if error is None else mask_numbers(error), "retry_after": retry_after,
              "state": link_state(conn, link_id, now), "accounts": _accounts(conn, link_id)}
             for link_id, name, country, link_status, valid_until, created_at, error, retry_after in conn.execute(
                "SELECT id, aspsp_name, aspsp_country, status, valid_until, created_at, last_error, retry_after "
                "FROM bank_links WHERE status IN ('active', 'expired') ORDER BY aspsp_name, id").fetchall()]
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
        "country": None if bank is None else bank.country,
        "redirect_url": None if bank is None else bank.redirect_url,
        "links": links, "alert": bank_status(conn, now), "runs": runs, "known_accounts": known})


@router.get("/aspsps")
def aspsps(request: Request, country: str = "FR"):
    """Banques proposées par Enable Banking dans un pays (noms seulement)."""
    return {"aspsps": _call(lambda: _runtime(request).aspsps(country))}


@router.post("/connect")
def connect_bank(request: Request, body: ConnectBody | None = None):
    body = body or ConnectBody()
    return {"url": _call(lambda: _runtime(request).start(body.aspsp_name, body.country))}


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
def sync_now(request: Request, body: SyncBody | None = None):
    link_id = None if body is None else body.link_id
    return _summary(_call(lambda: _runtime(request).sync("manuelle", link_id=link_id)))


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


@router.post("/accounts/{external_id}/kind")
def set_account_kind(external_id: int, body: KindBody, request: Request):
    _call(lambda: _runtime(request).write(lambda conn: _commit(conn, set_kind, external_id, body.kind)))
    return {"ok": True}


@router.post("/links/{link_id}/disconnect")
def disconnect_link(link_id: int, request: Request):
    _call(lambda: _runtime(request).disconnect(link_id))
    return {"ok": True}


@router.post("/disconnect")
def disconnect(request: Request):
    """Déconnecte toutes les banques."""
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


TAB = Tab("banque", "Connexions", 65, router)
