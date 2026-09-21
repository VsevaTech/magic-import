"""Magic Import — FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import router as api_router
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.services import import_service
from app.services.errors import MagicImportError
from app.services.schema_service import seed_builtin_schemas
from app.ui import router as ui_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("magic_import")

BASE_DIR = Path(__file__).resolve().parent

TAGS = [
    {"name": "Imports", "description": "Upload files and manage import jobs."},
    {"name": "Mapping", "description": "Automatic + manual column mapping, templates."},
    {"name": "Transformations", "description": "Value normalization rules and defaults."},
    {"name": "Validation", "description": "Run validation and browse issues."},
    {"name": "Review", "description": "Row-level review: inline edits, bulk fixes, undo."},
    {"name": "Export", "description": "CSV / XLSX / JSON exports and import reports."},
    {"name": "Schemas", "description": "Target data contracts (Schema Builder)."},
    {"name": "Templates", "description": "Saved mapping templates."},
    {"name": "Meta", "description": "Static reference data."},
]

DESCRIPTION = """
**Turn any spreadsheet into the data shape your system expects.**

Magic Import takes an arbitrary CSV/XLSX file, profiles it, maps its columns onto a
target *Import Schema*, normalizes values (phones, countries, dates, decimals…),
validates every row and exports an import-ready dataset.

Typical flow:

1. `POST /api/v1/imports` — upload a file
2. `POST /api/v1/imports/{id}/schema` — pick a schema, receive mapping suggestions
3. `POST /api/v1/imports/{id}/mapping` — confirm / adjust the mapping
4. `PUT  /api/v1/imports/{id}/transformations` — tune transformations (optional)
5. `POST /api/v1/imports/{id}/validate` — run transformation + validation
6. `GET  /api/v1/imports/{id}/rows`, `PATCH .../rows/{i}` — review and fix
7. `GET  /api/v1/imports/{id}/export?format=xlsx&scope=ready` — download

Every error uses one contract: `{"error": {"code", "message", "details"}}`.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    init_db()
    db = get_session_factory()()
    try:
        seed_builtin_schemas(db)
        removed = import_service.purge_old_uploads_for_jobs(db)
        removed += import_service.cleanup_uploads()
        if removed:
            log.info("cleanup: removed %d stale uploads", removed)
    finally:
        db.close()
    log.info(
        "Magic Import %s ready (AI %s, uploads deleted after processing: %s)",
        settings.app_version,
        "enabled" if settings.ai_available else "disabled",
        settings.delete_uploads_after_processing,
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Magic Import API",
        version=settings.app_version,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        contact={"name": "Magic Import", "url": "https://github.com/VsevaTech/magic-import"},
        license_info={"name": "MIT"},
    )

    @app.exception_handler(MagicImportError)
    async def domain_error(_request: Request, exc: MagicImportError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, exc: RequestValidationError):
        details = [
            {"loc": [str(x) for x in e.get("loc", [])], "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "details": details,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            code = {404: "not_found", 405: "method_not_allowed", 413: "file_too_large"}.get(
                exc.status_code, "http_error"
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": {"code": code, "message": str(exc.detail), "details": []}},
            )
        from app.ui import render_error

        return render_error(request, exc.status_code, str(exc.detail))

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(api_router)
    app.include_router(ui_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": settings.app_version}

    return app


app = create_app()
