"""Compte Kraken : section de l'onglet Connexions (clé API en lecture seule, avoirs en euros).

Les avoirs sont des relevés de portefeuille : ils se lisent dans GET /tabs/banque/wallets (provider 'kraken') et
dans l'onglet Épargne. Renommer ou retirer le compte passe par les routes des portefeuilles.
"""
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import get_conn
from app.sources.kraken.client import KrakenError, RateLimited
from app.sources.kraken.runtime import KeyInvalid, KrakenRuntime, NotConfigured, account_id
from app.sources.solana.runtime import RefreshBusy

router = APIRouter()


class KeyBody(BaseModel):
    api_key: str = Field(repr=False)
    api_secret: str = Field(repr=False)


def _runtime(request: Request) -> KrakenRuntime:
    return request.app.state.kraken


def _refresh(request: Request, trigger: str) -> dict:
    try:
        summary = _runtime(request).refresh(trigger)
    except NotConfigured as exc:
        raise HTTPException(503, "compte Kraken non relié") from exc
    except RefreshBusy as exc:
        raise HTTPException(409, "une actualisation est déjà en cours") from exc
    return {"refreshed": summary.refreshed, "errors": summary.errors}


@router.get("")
def status(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    runtime = _runtime(request)
    return {"configured": runtime.configured, "can_set_key": runtime.can_set_key, "account_id": account_id(conn)}


@router.put("/key")
def set_key(body: KeyBody, request: Request):
    """Clé Kraken saisie dans l'interface : vérifiée par une lecture des soldes, puis enregistrée. Jamais renvoyée."""
    try:
        _runtime(request).set_key(body.api_key, body.api_secret)
    except NotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except KeyInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    except RateLimited:
        raise HTTPException(503, "Kraken limite les requêtes : réessayez dans un moment") from None
    except KrakenError:
        raise HTTPException(502, "Kraken injoignable : réessayez plus tard") from None
    try:
        refresh = _refresh(request, "clé enregistrée")
    except HTTPException:
        refresh = None
    return {"ok": True, "refresh": refresh}


@router.delete("/key")
def delete_key(request: Request):
    _runtime(request).clear_key()
    return {"ok": True}


@router.post("/refresh")
def refresh(request: Request):
    return _refresh(request, "manuelle")
