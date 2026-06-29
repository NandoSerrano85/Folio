"""Folio portal FastAPI application.

The app shell owns: session middleware, static mount, Jinja2 templates, the
admin-user bootstrap, ``/health``, and ``GET /`` (serves the SPA shell). The
per-resource routers are Phase-2 leaf modules under ``portal.routers`` and are
included defensively so the shell still boots while they are being written.

Phase-2 routers MUST each expose an APIRouter named ``router`` at:

    portal.routers.auth       (POST/GET /api/auth/*)
    portal.routers.images     (GET /api/images*, file, thumb)
    portal.routers.folders    (CRUD /api/folders*)
    portal.routers.senders    (GET/POST/PATCH/DELETE /api/senders*)
    portal.routers.vendors    (GET/POST /api/vendors)
    portal.routers.accounts   (GET /api/accounts)
    portal.routers.download   (POST /api/download)
    portal.routers.stats      (GET /api/stats)
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from folio_core.config import get_settings
from folio_core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger("portal")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Routers to include, in order. Each is imported defensively.
_ROUTER_MODULES = (
    "auth",
    "images",
    "folders",
    "senders",
    "vendors",
    "accounts",
    "download",
    "stats",
    "collection_rules",
)


def _include_routers(app: FastAPI) -> None:
    for name in _ROUTER_MODULES:
        module_path = f"portal.routers.{name}"
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            logger.warning("router.missing module=%s (Phase-2)", module_path)
            continue
        router = getattr(module, "router", None)
        if router is None:
            logger.warning("router.no_router_attr module=%s", module_path)
            continue
        app.include_router(router)
        logger.info("router.included module=%s", module_path)


def _ensure_admin_user() -> None:
    """Create the admin user from env on first boot, if it does not exist."""
    from sqlalchemy import select

    from folio_core.db import session_scope
    from folio_core.models import User

    settings = get_settings()
    if not settings.admin_password:
        logger.warning("admin.bootstrap_skipped reason=ADMIN_PASSWORD not set")
        return

    try:
        from argon2 import PasswordHasher
    except ImportError:  # pragma: no cover - argon2 is a hard dep here
        logger.warning("admin.bootstrap_skipped reason=argon2-cffi missing")
        return

    try:
        with session_scope() as session:
            exists = session.scalar(
                select(User).where(User.username == settings.admin_username)
            )
            if exists is not None:
                return
            session.add(
                User(
                    username=settings.admin_username,
                    argon2_hash=PasswordHasher().hash(settings.admin_password),
                    is_active=True,
                )
            )
            logger.info("admin.created username=%s", settings.admin_username)
    except Exception:  # noqa: BLE001 - never block startup on a seeded user
        logger.exception("admin.bootstrap_failed (continuing)")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Folio", version="0.1.0")

    # The signed session cookie IS the portal's auth: a guessable signing key
    # lets anyone forge a logged-in cookie and bypass both the token and the
    # password path. Fail closed rather than fall back to a public default.
    if not settings.portal_secret_key:
        raise RuntimeError(
            "PORTAL_SECRET_KEY must be set — it signs the session cookie that "
            "carries login state. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    # Signed-cookie sessions for login state. ``session_https_only`` adds the
    # Secure flag; it defaults to False because the portal is typically reached
    # at http://NAS-IP:8899 on the LAN (set it True when fronted by HTTPS/VPN).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.portal_secret_key,
        session_cookie="folio_session",
        same_site="lax",
        https_only=settings.session_https_only,
    )

    # Ensure mountable dirs exist (Phase-2 frontend agent fills static/templates).
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    _include_routers(app)

    @app.on_event("startup")
    def _startup() -> None:
        _ensure_admin_user()
        logger.info("portal.startup complete")

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        index_file = TEMPLATES_DIR / "index.html"
        if index_file.exists():
            return templates.TemplateResponse(request, "index.html")
        # Minimal placeholder until the Phase-2 frontend ships index.html.
        return HTMLResponse(
            "<!doctype html><title>Folio</title>"
            "<h1>Folio</h1><p>Portal is running. UI pending.</p>"
        )

    return app


app = create_app()
