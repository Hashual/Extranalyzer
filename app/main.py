"""Assemble l'application web : authentification, onglets déclarés, dépôt et ré-analyse des relevés.

Lancement : `uvicorn --factory app.main:get_app` (rien n'est construit à l'import du module).
"""
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.analytics.totals import unclassified_count
from app.auth import SESSION_COOKIE, SESSION_MAX_AGE, RateLimiter, Sessions, load_or_create_secret, \
    verify_password
from app.classify.pipeline import on_new_transactions, reclassify
from app.classify.seed import is_fresh_install, load_seed
from app.config import Settings, load_settings
from app.deps import get_conn
from app.importer.creditmutuel import CreditMutuelParser
from app.importer.service import ImportReport, import_pdf, reanalyse_all, resolve_conflict
from app.model import ParseError
from app.store.db import connect
from app.tabs import ENABLED_TABS, load_tabs
from app.web_json import mask_numbers, report_to_json

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PARSERS = [CreditMutuelParser()]
SAFE_METHODS = frozenset({"GET", "HEAD"})
CSRF_HEADER = "x-extranalyzer"
MULTIPART_OVERHEAD = 64 * 1024     # marge d'en-têtes multipart tolérée au-delà de la taille maximale
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

T = TypeVar("T")


class UnknownSection(LookupError):
    pass


class LoginBody(BaseModel):
    password: str


class ResolveBody(BaseModel):
    accept: bool


def _safe_filename(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1].strip()
    return base[:255] or "releve.pdf"


def _content_length(request: Request) -> int | None:
    try:
        return int(request.headers["content-length"])
    except (KeyError, ValueError):
        return None


def _init_storage(settings: Settings) -> None:
    """Au démarrage : crée les dossiers, ouvre la base (schéma, migrations), charge le seed puis repasse la
    chaîne de classement une fois (une base mise à jour, un seuil modifié ou un nouveau seed prennent effet sans
    attendre un import ; une passe stable n'écrit rien).

    Les catégories du seed sont complétées à chaque démarrage, sauf celles que l'utilisateur a supprimées ou
    renommées (mémorisées dans `seed_dismissed`) ; ses règles ne sont installées que sur une installation neuve
    (une règle supprimée ou modifiée par l'utilisateur ne revient pas).
    """
    (settings.data_dir / "pdfs").mkdir(parents=True, exist_ok=True)
    conn = connect(settings.data_dir / "extranalyzer.db")
    try:
        with resources.as_file(resources.files("app.classify").joinpath("rules.seed.toml")) as seed:
            load_seed(conn, seed, with_rules=is_fresh_install(conn))
        reclassify(conn)
        conn.commit()
    finally:
        conn.close()


def _meta(conn: sqlite3.Connection) -> dict:
    accounts = [{"id": i, "label": label, "kind": kind, "number_tail": number[-4:]}
                for i, label, kind, number in
                conn.execute("SELECT id, label, kind, number FROM accounts ORDER BY id")]
    rows = conn.execute("SELECT id, name, parent_id, kind FROM categories ORDER BY id").fetchall()
    parents = {i: parent_id for i, _, parent_id, _ in rows}
    roots = {i: {"id": i, "name": name, "kind": kind, "children": []}
             for i, name, parent_id, kind in rows if parent_id is None}
    for i, name, parent_id, _ in rows:
        if parent_id is None:
            continue
        root, seen = parent_id, {i}
        while parents.get(root) is not None and root not in seen:   # remonte jusqu'à la racine
            seen.add(root)
            root = parents[root]
        if root in roots:
            roots[root]["children"].append({"id": i, "name": name})
        else:
            log.warning("catégorie %s ignorée : aucune racine (parent absent ou cycle)", i)
    unclassified = unclassified_count(conn)
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

    def api_guard(request: Request) -> JSONResponse | None:
        """Refus éventuel d'une requête /api avant routage (CSRF, session, taille du dépôt)."""
        path = request.url.path
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "en-tête X-Extranalyzer manquant"}, status_code=403)
        if path != "/api/login" and not sessions.valid(request.cookies.get(SESSION_COOKIE)):
            return JSONResponse({"detail": "authentification requise"}, status_code=401)
        if path == "/api/upload" and request.method == "POST":
            length = _content_length(request)
            if length is None:
                return JSONResponse({"detail": "en-tête Content-Length requis"}, status_code=411)
            if length > max_bytes + MULTIPART_OVERHEAD:
                return JSONResponse({"detail": f"fichier trop volumineux (maximum {settings.max_upload_mb} Mo)"},
                                    status_code=413)
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        response = (api_guard(request) if is_api else None) or await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        return response

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
        return report_to_json(report)

    @api.post("/sections/{section_id}/resolve")
    def resolve(section_id: int, body: ResolveBody):
        def run(conn: sqlite3.Connection) -> tuple[str, int]:
            if conn.execute("SELECT 1 FROM sections WHERE id = ?", (section_id,)).fetchone() is None:
                raise UnknownSection
            orphaned = resolve_conflict(conn, section_id, body.accept, on_new_transactions, pdf_dir=pdf_dir,
                                        parsers=PARSERS)
            return conn.execute("SELECT status FROM sections WHERE id = ?", (section_id,)).fetchone()[0], orphaned
        try:
            status, orphaned = write(run)
        except UnknownSection:
            raise HTTPException(404, f"section {section_id} introuvable") from None
        except (ValueError, ParseError) as exc:
            raise HTTPException(409, mask_numbers(str(exc))) from None
        return {"id": section_id, "status": status, "orphaned_overrides": orphaned}

    @api.post("/reanalyse")
    def reanalyse():
        reports = write(lambda conn: reanalyse_all(conn, pdf_dir, PARSERS, on_new_transactions))
        return [report_to_json(r) for r in reports]

    for tab in tabs:
        api.include_router(tab.router, prefix=f"/tabs/{tab.id}")
    app.include_router(api)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def get_app() -> FastAPI:
    return create_app(load_settings())
