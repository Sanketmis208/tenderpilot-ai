"""ASGI entry point: FastAPI (REST + MCP over streamable HTTP) with the NiceGUI web interface mounted on top."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text

from app.api.v1 import auth as auth_api
from app.api.v1 import bids as bids_api
from app.api.v1 import documents as documents_api
from app.api.v1 import misc as misc_api
from app.api.v1 import tenders as tenders_api
from app.core import ratelimit
from app.core.config import get_settings
from app.core.errors import AppError, RateLimited, Unauthenticated
from app.core.logging import configure_logging, get_logger, redact_path, request_id_var
from app.db.session import get_engine, session_scope
from app.mcp.server.app import mcp_server
from app.storage.backend import LocalStorage, get_storage

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
log = get_logger("app")

_host = urlsplit(settings.base_url).netloc
mcp_http_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=[_host, "localhost:*", "127.0.0.1:*"], allowed_origins=[settings.base_url]),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    import app.workers.tasks  # noqa: F401  registers job handlers in the web process (eager mode)

    app.workers.tasks._load_handlers()
    async with mcp_server.session_manager.run():
        log.info("startup", mode=settings.mode, storage=settings.storage_backend, llm=settings.llm_configured)
        yield


app = FastAPI(
    title="TenderPilot AI API",
    version="0.1.0",
    description="Tender intelligence, eligibility assessment and bid preparation. All endpoints are tenant-scoped.",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"],
                       allow_headers=["*", settings.csrf_header_name])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_var.set(rid[:64])
    started = time.monotonic()
    path = request.url.path
    try:
        if path.startswith(("/api/", "/mcp")):
            ip = request.client.host if request.client else "unknown"
            ratelimit.hit(f"ip:{ip}", settings.rate_limit_per_minute)
        response = await call_next(request)
    except RateLimited as exc:
        response = JSONResponse({"error": {"code": exc.code, "message": exc.message, "request_id": rid}}, status_code=429, headers={"Retry-After": "60"})
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/api/") and not path.startswith("/api/docs"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
    if not path.startswith(("/_nicegui", "/static")):
        log.info("http", method=request.method, path=redact_path(path), status=response.status_code, ms=int((time.monotonic() - started) * 1000))
    return response


@app.exception_handler(AppError)
async def app_error(_: Request, exc: AppError):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details, "request_id": request_id_var.get()}},
                        status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse({"error": {"code": "validation_failed", "message": "Request validation failed.",
                                   "details": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()], "request_id": request_id_var.get()}}, status_code=422)


for r in (auth_api.router, documents_api.router, tenders_api.router, bids_api.router, misc_api.router):
    app.include_router(r, prefix="/api/v1")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz():
    checks = {}
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    checks["redis"] = "ok" if ratelimit._redis() is not None else "unavailable (in-process rate limiting)"
    checks["storage"] = get_storage().name
    checks["llm"] = "configured" if settings.llm_configured else ("not configured (rule-based demo mode)" if settings.mode == "demo" else "MISSING")
    from app.integrations.antivirus import get_scanner

    av = get_scanner()
    checks["antivirus"] = ("clamav ok" if av.ping() else "clamav UNREACHABLE (uploads rejected)") if av.name == "clamav" else "not configured (uploads are not virus-scanned)"
    checks["ocr"] = "textract" if settings.textract_enabled else "disabled (scanned pages will be flagged, not read)"
    ok = checks["database"] == "ok"
    return JSONResponse({"status": "ready" if ok else "degraded", "mode": settings.mode, "checks": checks}, status_code=200 if ok else 503)


@app.get("/files/download", include_in_schema=False)
def local_download(key: str, exp: int, sig: str, fn: str = "file"):
    """Short-lived signed download for the local storage backend (S3 uses presigned URLs instead)."""
    if not LocalStorage.verify(key, exp, sig):
        return JSONResponse({"error": {"code": "forbidden", "message": "Link expired or invalid."}}, status_code=403)
    data = get_storage().get(key)
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}", "Cache-Control": "no-store"})


# ---- HTML form endpoints used by the NiceGUI auth pages (sets the HttpOnly session cookie) ----

def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    return not origin or urlsplit(origin).netloc == request.url.netloc


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/dashboard"


@app.post("/auth/form/login", include_in_schema=False)
def form_login(request: Request, email: str = Form(...), password: str = Form(...), next: str | None = Form(None)):
    if not _same_origin(request):
        return RedirectResponse("/login?error=origin", status_code=303)
    from app.services.organizations import auth_service

    try:
        ratelimit.hit(f"auth:{request.client.host if request.client else 'x'}", settings.auth_rate_limit_per_minute)
        with session_scope() as db:
            user = auth_service.authenticate(db, email, password)
            issued = auth_service.create_session(db, user, ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))
            has_org = issued.session.active_organization_id is not None
    except (Unauthenticated, RateLimited) as exc:
        return RedirectResponse(f"/login?error={quote(exc.message)}", status_code=303)
    resp = RedirectResponse(_safe_next(next) if has_org else "/onboarding", status_code=303)
    auth_api.set_session_cookie(resp, issued.token)
    return resp


@app.post("/auth/form/register", include_in_schema=False)
def form_register(request: Request, full_name: str = Form(...), email: str = Form(...), password: str = Form(...),
                  organization_name: str = Form(...), invite: str | None = Form(None)):
    if not _same_origin(request):
        return RedirectResponse("/register?error=origin", status_code=303)
    from app.services.organizations import auth_service, org_service

    try:
        ratelimit.hit(f"auth:{request.client.host if request.client else 'x'}", settings.auth_rate_limit_per_minute)
        with session_scope() as db:
            user = auth_service.register_user(db, email=email, password=password, full_name=full_name)
            if invite:
                org_service.accept_invitation(db, invite, user)
            elif organization_name.strip():
                org_service.create_organization(db, user, organization_name)
            db.flush()
            issued = auth_service.create_session(db, user, ip=request.client.host if request.client else None)
    except AppError as exc:
        return RedirectResponse(f"/register?error={quote(exc.message)}" + (f"&invite={quote(invite)}" if invite else ""), status_code=303)
    resp = RedirectResponse("/dashboard" if invite else "/onboarding", status_code=303)
    auth_api.set_session_cookie(resp, issued.token)
    return resp


@app.get("/auth/form/logout", include_in_schema=False)
def form_logout(request: Request):
    from app.services.organizations import auth_service

    with session_scope() as db:
        auth_service.revoke_session(db, request.cookies.get(settings.session_cookie_name))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(settings.session_cookie_name, path="/")
    return resp


# ---- MCP over streamable HTTP, guarded by API-token authentication ----

class MCPAuthGuard:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            ok = False
            if auth.lower().startswith("bearer "):
                from app.services.organizations.auth_service import resolve_api_token

                with session_scope() as db:
                    ok = resolve_api_token(db, auth[7:].strip(), channel="mcp_http") is not None
            if not ok:
                resp = JSONResponse({"error": {"code": "unauthenticated", "message": "MCP requires a valid TenderPilot API token."}},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app.mount("/mcp", MCPAuthGuard(mcp_http_app))

# ---- NiceGUI UI ----
from nicegui import ui  # noqa: E402

from app.ui import pages  # noqa: E402,F401  (registers @ui.page routes)

ui.run_with(app, title="TenderPilot AI", favicon="🧭", storage_secret=settings.secret_key.get_secret_value(), show_welcome_message=False)
