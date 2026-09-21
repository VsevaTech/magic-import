"""REST API v1."""

from __future__ import annotations

import io
import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import api as dto
from app.services import export_service, import_service, schema_service
from app.services.errors import FileTooLargeError, InvalidInputError
from app.services.mapping_engine import find_conflicts
from app.services.validation_engine import ISSUE_TITLES

router = APIRouter(prefix="/api/v1")
DB = Annotated[Session, Depends(get_db)]


def _job(db: Session, import_id: str):
    return import_service.get_job(db, import_id)


# ============================================================================ schemas
@router.get(
    "/schemas",
    response_model=list[dto.SchemaOut],
    tags=["Schemas"],
    summary="List import schemas",
)
def list_schemas(db: DB):
    return [schema_service.schema_to_dict(s) for s in schema_service.list_schemas(db)]


@router.post(
    "/schemas",
    response_model=dto.SchemaOut,
    status_code=201,
    tags=["Schemas"],
    summary="Create an import schema",
    responses={422: dto.ERROR_RESPONSES[422]},
)
def create_schema(payload: dto.SchemaIn, db: DB):
    schema = schema_service.create_schema(db, payload.model_dump())
    return schema_service.schema_to_dict(schema)


@router.get(
    "/schemas/{schema_id}",
    response_model=dto.SchemaOut,
    tags=["Schemas"],
    summary="Get a schema",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def get_schema(schema_id: str, db: DB):
    return schema_service.schema_to_dict(schema_service.get_schema(db, schema_id))


@router.put(
    "/schemas/{schema_id}",
    response_model=dto.SchemaOut,
    tags=["Schemas"],
    summary="Update a schema (fields are replaced when provided)",
    responses={404: dto.ERROR_RESPONSES[404], 422: dto.ERROR_RESPONSES[422]},
)
def update_schema(schema_id: str, payload: dto.SchemaUpdate, db: DB):
    schema = schema_service.update_schema(db, schema_id, payload.model_dump(exclude_none=True))
    return schema_service.schema_to_dict(schema)


@router.delete(
    "/schemas/{schema_id}",
    status_code=204,
    tags=["Schemas"],
    summary="Delete a schema",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def delete_schema(schema_id: str, db: DB):
    schema_service.delete_schema(db, schema_id)
    return Response(status_code=204)


# ============================================================================ imports
@router.get("/imports", response_model=dto.ImportListOut, tags=["Imports"], summary="List imports")
def list_imports(db: DB, limit: int = Query(50, ge=1, le=500)):
    return {"items": [import_service.job_to_dict(j) for j in import_service.list_jobs(db, limit)]}


@router.post(
    "/imports",
    response_model=dto.ImportOut,
    status_code=201,
    tags=["Imports"],
    summary="Upload a CSV/XLSX file and create an import job",
    description="Accepts `multipart/form-data` with a `file` part (CSV or XLSX, max 20 MB). "
    "The file is parsed, profiled (row/column counts, null %, probable types) and the "
    "raw upload is deleted unless it is a multi-sheet workbook that may need re-reading.",
    responses={400: dto.ERROR_RESPONSES[400], 413: dto.ERROR_RESPONSES[413]},
)
async def create_import(
    db: DB,
    file: UploadFile = File(..., description="CSV or XLSX file"),
    name: str | None = Form(None, description="Optional display name of the import"),
    sheet_name: str | None = Form(None, description="Sheet to read (XLSX only)"),
):
    settings = get_settings()
    data = await _read_limited(file, settings.max_upload_bytes)
    job = import_service.create_import(db, file.filename or "upload", data, name, sheet_name)
    return import_service.job_to_dict(job)


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise FileTooLargeError(f"File exceeds the {limit // (1024 * 1024)} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get(
    "/imports/{import_id}",
    response_model=dto.ImportOut,
    tags=["Imports"],
    summary="Get an import job (status, profile, summary)",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def get_import(import_id: str, db: DB):
    return import_service.job_to_dict(_job(db, import_id))


@router.delete(
    "/imports/{import_id}",
    status_code=204,
    tags=["Imports"],
    summary="Delete an import job",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def delete_import(import_id: str, db: DB):
    import_service.delete_job(db, import_id)
    return Response(status_code=204)


@router.post(
    "/imports/{import_id}/sheet",
    response_model=dto.ImportOut,
    tags=["Imports"],
    summary="Switch the sheet of an XLSX import",
    responses={404: dto.ERROR_RESPONSES[404], 409: dto.ERROR_RESPONSES[409]},
)
def select_sheet(import_id: str, payload: dto.SelectSheetIn, db: DB):
    job = import_service.select_sheet(db, _job(db, import_id), payload.sheet_name)
    return import_service.job_to_dict(job)


@router.post(
    "/imports/{import_id}/schema",
    response_model=dto.MappingOut,
    tags=["Mapping"],
    summary="Select the target schema and get automatic mapping suggestions",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def select_schema(import_id: str, payload: dto.SelectSchemaIn, db: DB):
    job = import_service.set_schema(db, _job(db, import_id), payload.schema_id, payload.ai_enabled)
    return _mapping_out(db, job)


def _mapping_out(db: Session, job) -> dict:
    tpl, score = import_service.detect_template(db, job)
    suggestion = None
    if tpl and job.template_id != tpl.id:
        suggestion = {"id": tpl.id, "name": tpl.name, "match": round(score, 2)}
    targets = import_service._targets(job)
    return {
        "mapping": import_service.mapping_view(db, job),
        "conflicts": find_conflicts(import_service.current_mapping(job), targets),
        "template_suggestion": suggestion,
    }


@router.get(
    "/imports/{import_id}/mapping",
    response_model=dto.MappingOut,
    tags=["Mapping"],
    summary="Current mapping with confidence levels",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def get_mapping(import_id: str, db: DB):
    return _mapping_out(db, _job(db, import_id))


@router.post(
    "/imports/{import_id}/mapping",
    response_model=dto.MappingOut,
    tags=["Mapping"],
    summary="Update / confirm the mapping",
    description="Send `source column → target field` pairs (`null` ignores a column). "
    "Mapping a target twice returns `invalid_mapping` unless the field allows multiple "
    "sources. With `confirm=true` the pairs are remembered for future imports and "
    "default transformations are proposed.",
    responses={404: dto.ERROR_RESPONSES[404], 422: dto.ERROR_RESPONSES[422]},
)
def update_mapping(import_id: str, payload: dto.MappingIn, db: DB):
    job = import_service.update_mapping(db, _job(db, import_id), payload.mapping, payload.confirm)
    return _mapping_out(db, job)


@router.post(
    "/imports/{import_id}/mapping/apply-template",
    response_model=dto.MappingOut,
    tags=["Mapping"],
    summary="Apply a saved mapping template",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def apply_template(import_id: str, payload: dto.ApplyTemplateIn, db: DB):
    job = import_service.apply_template(db, _job(db, import_id), payload.template_id)
    return _mapping_out(db, job)


@router.post(
    "/imports/{import_id}/mapping/save-template",
    response_model=dto.TemplateOut,
    status_code=201,
    tags=["Mapping"],
    summary="Save the current mapping as a reusable template",
    responses={404: dto.ERROR_RESPONSES[404], 409: dto.ERROR_RESPONSES[409]},
)
def save_template(import_id: str, payload: dto.TemplateIn, db: DB):
    tpl = import_service.save_template(db, _job(db, import_id), payload.name)
    return _template_out(tpl)


def _template_out(t) -> dict:
    return {
        "id": t.id,
        "schema_id": t.schema_id,
        "name": t.name,
        "source_columns": t.source_columns,
        "mapping": t.mapping,
        "use_count": t.use_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@router.get(
    "/imports/{import_id}/transformations",
    response_model=list[dto.TransformationOut],
    tags=["Transformations"],
    summary="Transformations configured for this import",
)
def get_transformations(import_id: str, db: DB):
    return import_service.transformations_view(_job(db, import_id))


@router.put(
    "/imports/{import_id}/transformations",
    response_model=list[dto.TransformationOut],
    tags=["Transformations"],
    summary="Replace the transformation list",
    responses={409: dto.ERROR_RESPONSES[409], 422: dto.ERROR_RESPONSES[422]},
)
def set_transformations(import_id: str, payload: dto.TransformationsIn, db: DB):
    job = import_service.set_transformations(
        db, _job(db, import_id), [t.model_dump() for t in payload.transformations]
    )
    return import_service.transformations_view(job)


@router.put(
    "/imports/{import_id}/defaults",
    response_model=dto.ImportOut,
    tags=["Transformations"],
    summary="Set default values for target fields without a source column",
)
def set_defaults(import_id: str, payload: dto.DefaultsIn, db: DB):
    return import_service.job_to_dict(
        import_service.set_defaults(db, _job(db, import_id), payload.defaults)
    )


@router.post(
    "/imports/{import_id}/validate",
    response_model=dto.ImportOut,
    tags=["Validation"],
    summary="Run transformations + validation over the whole dataset",
    responses={409: dto.ERROR_RESPONSES[409]},
)
def validate(import_id: str, db: DB):
    return import_service.job_to_dict(import_service.run_validation(db, _job(db, import_id)))


@router.get(
    "/imports/{import_id}/issues",
    response_model=dto.IssuesOut,
    tags=["Validation"],
    summary="Validation issues (paged, filterable)",
)
def get_issues(
    import_id: str,
    db: DB,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    severity: str | None = Query(None, pattern="^(?i)(error|warning|info)$"),
    field: str | None = None,
    code: str | None = None,
):
    return import_service.issues_page(
        db, _job(db, import_id), page, page_size, severity, field, code
    )


@router.get(
    "/imports/{import_id}/rows",
    response_model=dto.RowsOut,
    tags=["Review"],
    summary="Normalized rows with original values and cell-level issues (paged)",
    responses={409: dto.ERROR_RESPONSES[409]},
)
def get_rows(
    import_id: str,
    db: DB,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status: str | None = Query(None, pattern="^(ready|warning|error|issues)$"),
    field: str | None = None,
    code: str | None = None,
    search: str | None = None,
):
    return import_service.rows_page(
        db, _job(db, import_id), page, page_size, status, field, code, search
    )


@router.patch(
    "/imports/{import_id}/rows/{row_index}",
    response_model=dto.RowOut,
    tags=["Review"],
    summary="Edit one cell and revalidate the row",
    responses={404: dto.ERROR_RESPONSES[404], 409: dto.ERROR_RESPONSES[409]},
)
def edit_cell(import_id: str, row_index: int, payload: dto.CellEditIn, db: DB):
    return import_service.edit_cell(
        db, _job(db, import_id), row_index, payload.field, payload.value
    )


@router.get(
    "/imports/{import_id}/bulk-fixes",
    tags=["Review"],
    summary="Suggested bulk fixes (e.g. normalize a misspelled country for N rows)",
)
def bulk_fixes(import_id: str, db: DB) -> list[dict]:
    return import_service.bulk_fix_suggestions(db, _job(db, import_id))


@router.post(
    "/imports/{import_id}/bulk-fixes/replace",
    response_model=dto.OkOut,
    tags=["Review"],
    summary="Replace a value in a column for all matching rows",
)
def bulk_replace(import_id: str, payload: dto.BulkReplaceIn, db: DB):
    res = import_service.bulk_replace(
        db,
        _job(db, import_id),
        payload.field,
        payload.from_value,
        payload.to_value,
        payload.match_original,
    )
    return {"ok": True, "detail": res}


@router.post(
    "/imports/{import_id}/bulk-fixes/trim",
    response_model=dto.OkOut,
    tags=["Review"],
    summary="Trim whitespace in a column for all rows",
)
def bulk_trim(import_id: str, payload: dto.BulkTrimIn, db: DB):
    return {"ok": True, "detail": import_service.bulk_trim(db, _job(db, import_id), payload.field)}


@router.post(
    "/imports/{import_id}/undo",
    response_model=dto.OkOut,
    tags=["Review"],
    summary="Undo the last manual change (cell edit or bulk fix)",
)
def undo(import_id: str, db: DB):
    return {"ok": True, "detail": import_service.undo_last(db, _job(db, import_id))}


@router.post(
    "/imports/{import_id}/reset-column",
    response_model=dto.OkOut,
    tags=["Review"],
    summary="Drop all manual edits of a column",
)
def reset_column(import_id: str, payload: dto.ResetColumnIn, db: DB):
    return {
        "ok": True,
        "detail": import_service.reset_column(db, _job(db, import_id), payload.field),
    }


@router.post(
    "/imports/{import_id}/complete",
    response_model=dto.ImportOut,
    tags=["Imports"],
    summary="Mark the import as completed",
)
def complete(import_id: str, db: DB):
    return import_service.job_to_dict(import_service.mark_completed(db, _job(db, import_id)))


# ============================================================================ export
@router.get(
    "/imports/{import_id}/export",
    tags=["Export"],
    summary="Download the normalized dataset",
    description="`format` = csv | xlsx | json | errors (errors.csv with an `_issues` column). "
    "`scope` = ready (default: rows without errors) | all | errors. Values starting with "
    "`= + - @` are neutralised against spreadsheet formula injection.",
    responses={
        200: {
            "content": {
                "text/csv": {},
                "application/json": {},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
            }
        },
        409: dto.ERROR_RESPONSES[409],
    },
)
def export(
    import_id: str,
    db: DB,
    format: str = Query("csv", pattern="^(csv|xlsx|json|errors)$"),
    scope: str = Query("ready", pattern="^(ready|all|errors)$"),
):
    job = _job(db, import_id)
    import_service._require_validated(job)
    field_names = [f.name for f in job.schema.fields]
    base = job.original_filename.rsplit(".", 1)[0] or "export"
    if format == "errors":
        selected = export_service.select_rows(job.normalized_rows, job.row_status, "errors")
        issues = import_service.issues_page(db, job, 1, 100_000, "ERROR")["items"]
        by_row: dict[int, list] = {}
        for iss in issues:
            if iss["row_index"] is not None:
                by_row.setdefault(iss["row_index"], []).append(iss)
        data = export_service.errors_csv(field_names, selected, by_row)
        return _download(data, f"{base}-errors.csv", "text/csv; charset=utf-8")
    selected = export_service.select_rows(job.normalized_rows, job.row_status, scope)
    rows = [rec for _, rec in selected]
    if format == "csv":
        return _download(
            export_service.to_csv(field_names, rows),
            f"{base}-normalized.csv",
            "text/csv; charset=utf-8",
        )
    if format == "json":
        return _download(
            export_service.to_json(field_names, rows), f"{base}-normalized.json", "application/json"
        )
    report = _report(db, job, scope, len(rows))
    data = export_service.to_xlsx(field_names, rows, report)
    return _download(
        data,
        f"{base}-normalized.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get(
    "/imports/{import_id}/report",
    tags=["Export"],
    summary="Import report (JSON): source, schema, counts, mapping, transformations, issues",
    responses={409: dto.ERROR_RESPONSES[409]},
)
def report(import_id: str, db: DB, download: bool = False):
    job = _job(db, import_id)
    import_service._require_validated(job)
    rep = _report(db, job, "ready", job.summary.get("importable", 0))
    if download:
        return _download(
            json.dumps(rep, ensure_ascii=False, indent=2).encode(),
            "import-report.json",
            "application/json",
        )
    return rep


def _report(db: Session, job, scope: str, exported_rows: int) -> dict:
    mapping = [
        {
            "source": m.source_column,
            "target": None if m.ignored else m.target_field,
            "confidence": m.confidence,
            "method": m.method,
        }
        for m in job.mappings
    ]
    transformations = [
        {"kind": t.kind, "label": t.label, "target_field": t.target_field}
        for t in job.transformations
        if t.enabled
    ]
    return export_service.build_report(
        job,
        job.summary,
        scope,
        exported_rows,
        mapping,
        transformations,
        job.summary.get("top_issues", []),
    )


def _download(data: bytes, filename: str, media_type: str) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================ templates
@router.get(
    "/templates",
    response_model=list[dto.TemplateOut],
    tags=["Templates"],
    summary="List mapping templates",
)
def list_templates(db: DB, schema_id: str | None = None):
    return [_template_out(t) for t in import_service.list_templates(db, schema_id)]


@router.delete(
    "/templates/{template_id}",
    status_code=204,
    tags=["Templates"],
    summary="Delete a mapping template",
    responses={404: dto.ERROR_RESPONSES[404]},
)
def delete_template(template_id: str, db: DB):
    import_service.delete_template(db, template_id)
    return Response(status_code=204)


# ============================================================================ meta
@router.get(
    "/meta",
    tags=["Meta"],
    summary="Field types, transformation kinds, issue codes, AI availability",
)
def meta():
    from app.services.schema_service import FIELD_TYPES
    from app.services.transformation_engine import KIND_LABELS

    settings = get_settings()
    return {
        "field_types": FIELD_TYPES,
        "transformation_kinds": KIND_LABELS,
        "issue_codes": ISSUE_TITLES,
        "ai_available": settings.ai_available,
        "ai_model": settings.gemini_model if settings.ai_available else None,
        "max_upload_mb": settings.max_upload_mb,
        "version": settings.app_version,
    }


__all__ = ["InvalidInputError", "router"]
