"""Server-rendered UI (Jinja2 + Alpine.js + Tailwind). All data operations go through
the JSON API, so the UI is a thin client over the same contract the API exposes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import import_service, schema_service
from app.services.errors import NotFoundError
from app.services.import_service import STEPS
from app.services.schema_service import FIELD_TYPES
from app.services.transformation_engine import KIND_LABELS

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_SPEC = BASE_DIR.parent / "demo-data" / "merchant-api.yaml"
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["tojson_attr"] = lambda v: json.dumps(v, ensure_ascii=False)

router = APIRouter(include_in_schema=False)
DB = Annotated[Session, Depends(get_db)]


def _ctx(request: Request, **kwargs) -> dict:
    settings = get_settings()
    return {
        "request": request,
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "ai_available": settings.ai_available,
        "max_upload_mb": settings.max_upload_mb,
        "steps": STEPS,
        **kwargs,
    }


def render_error(request: Request, status: int, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", _ctx(request, status=status, message=message), status_code=status
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DB):
    jobs = import_service.list_jobs(db, 8)
    all_jobs = import_service.list_jobs(db, 500)
    schemas = schema_service.list_schemas(db)
    total_rows = sum(j.row_count for j in all_jobs)
    completed = sum(1 for j in all_jobs if j.status == "completed")
    needs_review = sum(1 for j in all_jobs if j.status in ("validated", "mapped", "mapping"))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            jobs=[import_service.job_to_dict(j) for j in jobs],
            schemas=schemas,
            stats={
                "imports": len(all_jobs),
                "rows": total_rows,
                "completed": completed,
                "needs_review": needs_review,
                "schemas": len(schemas),
            },
            active="dashboard",
        ),
    )


@router.get("/imports", response_class=HTMLResponse)
def history(request: Request, db: DB):
    jobs = import_service.list_jobs(db, 200)
    return templates.TemplateResponse(
        request,
        "history.html",
        _ctx(request, jobs=[import_service.job_to_dict(j) for j in jobs], active="imports"),
    )


@router.get("/imports/new", response_class=HTMLResponse)
def new_import(request: Request):
    return templates.TemplateResponse(request, "upload.html", _ctx(request, active="imports"))


@router.get("/imports/{import_id}", response_class=HTMLResponse)
def import_wizard(request: Request, import_id: str, db: DB, step: int | None = None):
    try:
        job = import_service.get_job(db, import_id)
    except NotFoundError:
        return render_error(request, 404, "This import does not exist (it may have been deleted).")
    max_step = {
        "uploaded": 2,
        "mapping": 3,
        "mapped": 4,
        "validated": 7,
        "completed": 7,
    }.get(job.status, 2)
    current = step or min(max(job.step, 2), max_step)
    current = max(2, min(current, max_step))
    schemas = schema_service.list_schemas(db)
    schema = schema_service.schema_to_dict(job.schema) if job.schema else None
    return templates.TemplateResponse(
        request,
        "wizard.html",
        _ctx(
            request,
            job=import_service.job_to_dict(job),
            schema=schema,
            schemas=[schema_service.schema_to_dict(s) for s in schemas],
            current=current,
            max_step=max_step,
            kind_labels=KIND_LABELS,
            field_types=FIELD_TYPES,
            active="imports",
        ),
    )


@router.get("/schemas", response_class=HTMLResponse)
def schemas_page(request: Request, db: DB, created: str | None = None):
    schemas = [schema_service.schema_to_dict(s) for s in schema_service.list_schemas(db)]
    templates_ = [
        {
            "id": t.id,
            "name": t.name,
            "schema_id": t.schema_id,
            "use_count": t.use_count,
            "columns": t.source_columns,
        }
        for t in import_service.list_templates(db)
    ]
    return templates.TemplateResponse(
        request,
        "schemas.html",
        _ctx(
            request,
            schemas=schemas,
            mapping_templates=templates_,
            created=created,
            active="schemas",
        ),
    )


@router.get("/schemas/new", response_class=HTMLResponse)
def new_schema(request: Request):
    return templates.TemplateResponse(
        request,
        "schema_builder.html",
        _ctx(request, schema=None, field_types=FIELD_TYPES, active="schemas"),
    )


@router.get("/schemas/from-contract", response_class=HTMLResponse)
def schema_from_contract(request: Request):
    return templates.TemplateResponse(
        request,
        "schema_from_contract.html",
        _ctx(
            request,
            field_types=FIELD_TYPES,
            sample_available=SAMPLE_SPEC.is_file(),
            active="schemas",
        ),
    )


@router.get("/samples/merchant-api.yaml")
def sample_spec(request: Request):
    if not SAMPLE_SPEC.is_file():
        return render_error(request, 404, "The sample contract is not shipped with this build.")
    return FileResponse(SAMPLE_SPEC, media_type="text/yaml; charset=utf-8")


@router.get("/schemas/{schema_id}", response_class=HTMLResponse)
def edit_schema(request: Request, schema_id: str, db: DB):
    try:
        schema = schema_service.get_schema(db, schema_id)
    except NotFoundError:
        return render_error(request, 404, "Schema not found.")
    return templates.TemplateResponse(
        request,
        "schema_builder.html",
        _ctx(
            request,
            schema=schema_service.schema_to_dict(schema),
            field_types=FIELD_TYPES,
            active="schemas",
        ),
    )


@router.get("/history")
def history_redirect():
    return RedirectResponse("/imports", status_code=307)
