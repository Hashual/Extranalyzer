"""Portefeuilles Solana : section de l'onglet Banque (suivi en lecture seule, valeurs en euros)."""
import sqlite3
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import get_conn
from app.sources.solana import wallets
from app.sources.solana.client import HeliusError, RateLimited
from app.sources.solana.runtime import (KeyInvalid, KeyManagedByServer, NotConfigured, RefreshBusy,
                                        SolanaRuntime)
from app.web_json import to_jsonable

router = APIRouter()


class WalletBody(BaseModel):
    label: str
    address: str


class LabelBody(BaseModel):
    label: str


class RefreshBody(BaseModel):
    wallet_id: int | None = None


class HiddenBody(BaseModel):
    hidden: bool


class KeyBody(BaseModel):
    api_key: str = Field(repr=False)


def _runtime(request: Request) -> SolanaRuntime:
    return request.app.state.solana


def _commit(conn: sqlite3.Connection, fn, *args):
    try:
        result = fn(conn, *args)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def _write(request: Request, fn, *args):
    """Écriture sous le verrou de l'appli ; les erreurs de saisie deviennent des réponses HTTP."""
    try:
        return _runtime(request).write(lambda conn: _commit(conn, fn, *args))
    except wallets.DuplicateWallet as exc:
        raise HTTPException(409, str(exc)) from exc
    except wallets.WalletError as exc:
        raise HTTPException(422, str(exc)) from exc


def _address(raw: str) -> str:
    try:
        return wallets.clean_address(raw)
    except wallets.WalletError as exc:
        raise HTTPException(422, str(exc)) from exc


def _refresh(request: Request, trigger: str, wallet_id: int | None = None) -> dict:
    try:
        summary = _runtime(request).refresh(trigger, wallet_id)
    except NotConfigured as exc:
        raise HTTPException(503, "portefeuilles non configurés") from exc
    except RefreshBusy as exc:
        raise HTTPException(409, "une actualisation est déjà en cours") from exc
    return {"refreshed": summary.refreshed, "errors": summary.errors}


def _exists(conn: sqlite3.Connection, wallet_id: int) -> bool:
    return conn.execute("SELECT 1 FROM wallets WHERE id = ?", (wallet_id,)).fetchone() is not None


def _exact(value):
    """Quantité ou prix en chaîne décimale non arrondie (to_jsonable arrondirait à 2 décimales)."""
    return None if value is None else format(value, "f")


@router.get("")
def list_wallets(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    data = wallets.overview(conn)
    for wallet in data["wallets"]:
        for position in wallet["positions"]:
            position["amount"] = _exact(position["amount"])
            position["price_eur"] = _exact(position["price_eur"])
    runtime = _runtime(request)
    return to_jsonable({"configured": runtime.configured, "key_source": runtime.key_source,
                        "can_set_key": runtime.can_set_key, **data})


@router.put("/key")
def set_key(body: KeyBody, request: Request):
    """Clé Helius saisie dans l'interface : vérifiée par un appel en lecture, puis enregistrée. Jamais renvoyée."""
    runtime = _runtime(request)
    try:
        runtime.set_key(body.api_key)
    except KeyManagedByServer as exc:
        raise HTTPException(409, str(exc)) from None
    except NotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except KeyInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    except RateLimited:
        raise HTTPException(503, "Helius limite les requêtes : réessayez dans un moment") from None
    except HeliusError:
        raise HTTPException(502, "Helius injoignable : réessayez plus tard") from None
    refresh = None
    try:
        refresh = _refresh(request, "clé enregistrée")
    except HTTPException:
        refresh = None
    return {"ok": True, "refresh": refresh}


@router.delete("/key")
def delete_key(request: Request):
    try:
        _runtime(request).clear_key()
    except KeyManagedByServer as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@router.post("")
def add_wallet(body: WalletBody, request: Request):
    now = datetime.now(timezone.utc).isoformat()
    wallet_id = _write(request, wallets.add_wallet, body.label, body.address, now)
    refresh = None
    if _runtime(request).configured:
        try:
            refresh = _refresh(request, "ajout", wallet_id)
        except HTTPException:
            refresh = None      # le portefeuille est ajouté ; l'actualisation se refera à la main
    return {"id": wallet_id, "refresh": refresh}


@router.post("/refresh")
def refresh_wallets(body: RefreshBody, request: Request):
    return _refresh(request, "manuelle", body.wallet_id)


@router.patch("/{wallet_id}")
def rename_wallet(wallet_id: int, body: LabelBody, request: Request):
    _write(request, _checked(wallets.rename_wallet), wallet_id, body.label)
    return {"ok": True}


@router.delete("/{wallet_id}")
def remove_wallet(wallet_id: int, request: Request):
    _write(request, _checked(wallets.remove_wallet), wallet_id)
    return {"ok": True}


def _checked(fn):
    """Même opération, après avoir vérifié que le portefeuille existe (404 sinon)."""
    def run(conn, wallet_id, *args):
        if not _exists(conn, wallet_id):
            raise HTTPException(404, "portefeuille introuvable")
        return fn(conn, wallet_id, *args)
    return run


@router.put("/validators/{vote_account}")
def label_validator(vote_account: str, body: LabelBody, request: Request):
    _write(request, wallets.set_validator_label, _address(vote_account), body.label)
    return {"ok": True}


@router.put("/hidden-mints/{mint}")
def hide_mint(mint: str, body: HiddenBody, request: Request):
    _write(request, wallets.hide_mint, _address(mint), body.hidden)
    return {"ok": True}
