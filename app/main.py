"""Assemble l'application web : authentification, onglets déclarés, dépôt et ré-analyse des relevés.

Lancement : `uvicorn --factory app.main:get_app` (rien n'est construit à l'import du module).
"""
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from importlib import resources
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import SESSION_COOKIE, SESSION_MAX_AGE, RateLimiter, Sessions, load_or_create_secret, \
    verify_password
from app.classify.pipeline import on_new_transactions
from app.classify.seed import load_seed
from app.config import Settings, load_settings
from app.importer.creditmutuel import CreditMutuelParser
from app.importer.service import ImportReport, import_pdf, reanalyse_all, resolve_conflict
from app.model import ParseError
from app.store.db import connect
from app.tabs import ENABLED_TABS, load_tabs
from app.web_json import to_jsonable

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PARSERS = [CreditMutuelParser()]
SAFE_METHODS = frozenset({"GET", "HEAD"})
CSRF_HEADER = "x-extranalyzer"
MULTIPART_OVERHEAD = 64 * 1024     # marge d'en-têtes multipart tolérée au-delà de la taille maximale

T = TypeVar("T")


class LoginBody(BaseModel):
    password: str


class ResolveBody(BaseModel):
    accept: bool


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Dépendance : une connexion par requête (ouverte/fermée par FastAPI, éventuellement dans
    des threads différents de celui de la route, d'où `check_same_thread=False`)."""
    conn = connect(request.app.state.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _safe_filename(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1].strip()
    return base[:255] or "releve.pdf"


def _content_length(request: Request) -> int:
    try:
        return int(request.headers.get("content-length", "0"))
    except ValueError:
        return 0


def _init_storage(settings: Settings) -> None:
    """Au démarrage : crée les dossiers, ouvre la base (schéma) puis charge le seed."""
    (settings.data_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    conn = connect(settings.data_dir / "extranalyzer.db")
    try:
        with resources.as_file(resources.files("app.classify").joinpath("rules.seed.toml")) as seed:
            load_seed(conn, seed)
    finally:
        conn.close()


def _meta(conn: sqlite3.Connection) -> dict:
    accounts = [{"id": i, "label": label, "kind": kind, "number_tail": number[-4:]}
                for i, label, kind, number in
                conn.execute("SELECT id, label, kind, number FROM accounts ORDER BY id")]
    roots: dict[int, dict] = {}
    children: list[tuple] = []
    for i, name, parent_id, kind in conn.execute("SELECT id, name, parent_id, kind FROM categories ORDER BY id"):
        if parent_id is None:
            roots[i] = {"id": i, "name": name, "kind": kind, "children": []}
        else:
            children.append((i, name, parent_id))
    for i, name, parent_id in children:
        roots[parent_id]["children"].append({"id": i, "name": name})
    unclassified = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND mirror_of IS NULL").fetchone()[0]
    low, high = conn.execute("SELECT MIN(op_date), MAX(op_date) FROM transactions").fetchone()
    return {"accounts": accounts, "categories": list(roots.values()), "unclassified_count": unclassified,
            "data_range": {"min": low, "max": high}}


def create_app(settings: Settings, enabled_tabs: list[str] | None = None) -> FastAPI:
    tabs = load_tabs(ENABLED_TABS if enabled_tabs is None else enabled_tabs)
    _init_storage(settings)
    db_path, pdf_dir = settings.data_dir / "extranalyzer.db", settings.data_dir / "pdfs"
    sessions = Sessions(load_or_create_secret(settings.data_dir / "secret.key"))
    limiter = RateLimiter(limit=5, window=60)
    write_lock = threading.Lock()      # une seule écriture lourde (import, conflit, ré-analyse) à la fois
    max_bytes = settings.max_upload_mb * 1024 * 1024

    def write(fn: Callable[[sqlite3.Connection], T]) -> T:
        with write_lock:
            conn = connect(db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

    app = FastAPI(title="Extranalyzer", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = db_path

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": "en-tête X-Extranalyzer manquant"}, status_code=403)
            if path != "/api/login" and not sessions.valid(request.cookies.get(SESSION_COOKIE)):
                return JSONResponse({"detail": "authentification requise"}, status_code=401)
            if path == "/api/upload" and _content_length(request) > max_bytes + MULTIPART_OVERHEAD:
                return JSONResponse({"detail": f"fichier trop volumineux (maximum {settings.max_upload_mb} Mo)"},
                                    status_code=413)
        return await call_next(request)

    api = APIRouter(prefix="/api")

    @api.post("/login")
    def login(body: LoginBody, request: Request, response: Response):
        ip = request.client.host if request.client else "inconnue"
        if not limiter.acquire(ip):
            raise HTTPException(429, "trop de tentatives, réessayez dans une minute")
        if not verify_password(body.password, settings.password_hash):
            raise HTTPException(401, "mot de passe incorrect")
        limiter.succeeded(ip)
        response.set_cookie(SESSION_COOKIE, sessions.issue(), max_age=SESSION_MAX_AGE, path="/",
                            httponly=True, samesite="strict", secure=False)
        return {"ok": True}

    @api.post("/logout")
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=False)
        return {"ok": True}

    @api.get("/tabs")
    def list_tabs():
        return [{"id": t.id, "title": t.title, "order": t.order} for t in tabs]

    @api.get("/meta")
    def meta(conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
        return _meta(conn)

    def _import(data: bytes, filename: str) -> ImportReport:
        try:
            return write(lambda conn: import_pdf(conn, data, filename, pdf_dir, PARSERS, on_new_transactions))
        except Exception as exc:     # fichier hostile ou inattendu : rapport d'erreur, le serveur continue
            log.error("échec de l'import d'un relevé (%s)", type(exc).__name__)
            return ImportReport(None, False, [], "échec de l'analyse du relevé")

    @api.post("/upload")
    async def upload(file: Annotated[UploadFile, File()]):
        data = await file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HTTPException(413, f"fichier trop volumineux (maximum {settings.max_upload_mb} Mo)")
        if b"%PDF-" not in data[:1024]:
            report = ImportReport(None, False, [], "le fichier n'est pas un PDF")
        else:
            report = await run_in_threadpool(_import, data, _safe_filename(file.filename))
        return to_jsonable(report)

    @api.post("/sections/{section_id}/resolve")
    def resolve(section_id: int, body: ResolveBody):
        def run(conn: sqlite3.Connection) -> str:
            resolve_conflict(conn, section_id, body.accept, on_new_transactions, pdf_dir=pdf_dir, parsers=PARSERS)
            return conn.execute("SELECT status FROM sections WHERE id = ?", (section_id,)).fetchone()[0]
        try:
            status = write(run)
        except (ValueError, ParseError) as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": section_id, "status": status}

    @api.post("/reanalyse")
    def reanalyse():
        return to_jsonable(write(lambda conn: reanalyse_all(conn, pdf_dir, PARSERS, on_new_transactions)))

    for tab in tabs:
        api.include_router(tab.router, prefix=f"/tabs/{tab.id}")
    app.include_router(api)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def get_app() -> FastAPI:
    return create_app(load_settings())
