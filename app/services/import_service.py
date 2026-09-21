"""Import job orchestration: upload -> inspect -> map -> transform -> validate -> export."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ImportJob,
    Mapping,
    MappingMemory,
    MappingTemplate,
    Transformation,
    ValidationIssue,
)
from app.services import file_parser
from app.services import normalizers as norm
from app.services.ai.gemini import build_provider
from app.services.errors import (
    FileTooLargeError,
    InvalidFileError,
    InvalidInputError,
    InvalidMappingError,
    InvalidStateError,
    NotFoundError,
)
from app.services.mapping_engine import (
    MappingEngine,
    SourceColumn,
    TargetField,
    find_conflicts,
    normalize_header,
    template_match_score,
)
from app.services.schema_service import get_schema
from app.services.transformation_engine import (
    KIND_LABELS,
    TransformationEngine,
    TransformSpec,
    suggest_transformations,
)
from app.services.validation_engine import (
    ISSUE_TITLES,
    TYPE_ISSUES,
    Issue,
    ValidationEngine,
    top_issues,
)

log = logging.getLogger("magic_import")

STEPS = ["Upload", "Inspect", "Map", "Transform", "Validate", "Review", "Export"]
TEMPLATE_MATCH_THRESHOLD = 0.8

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ----------------------------------------------------------------------------- helpers
def safe_filename(original: str) -> str:
    """Server-side filename: random prefix + sanitized basename, no path parts."""
    base = original.replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._") or "upload"
    base = base[-80:]
    return f"{uuid.uuid4().hex}_{base}"


def get_job(db: Session, job_id: str) -> ImportJob:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError(f"Import '{job_id}' not found.")
    return job


def list_jobs(db: Session, limit: int = 50) -> list[ImportJob]:
    stmt = select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def delete_job(db: Session, job_id: str) -> None:
    job = get_job(db, job_id)
    _delete_upload(job)
    db.delete(job)
    db.commit()


def _upload_path(job: ImportJob) -> Path | None:
    if not job.stored_filename:
        return None
    return get_settings().resolved_upload_dir / job.stored_filename


def _delete_upload(job: ImportJob) -> None:
    path = _upload_path(job)
    if path and path.exists():
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            log.warning("could not delete upload %s", path.name)
    job.stored_filename = None


def cleanup_uploads(hours: int | None = None) -> int:
    """Delete raw uploads older than the retention window. Returns count removed."""
    settings = get_settings()
    hours = settings.upload_retention_hours if hours is None else hours
    cutoff = time.time() - hours * 3600
    removed = 0
    for p in settings.resolved_upload_dir.glob("*"):
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


# ----------------------------------------------------------------------------- upload
def create_import(
    db: Session,
    filename: str,
    data: bytes,
    name: str | None = None,
    sheet_name: str | None = None,
) -> ImportJob:
    settings = get_settings()
    ext = file_parser.extension_of(filename)
    if ext not in file_parser.ALLOWED_EXTENSIONS:
        raise InvalidFileError(
            "Only .csv and .xlsx files are supported.", details=[{"extension": ext}]
        )
    if len(data) > settings.max_upload_bytes:
        raise FileTooLargeError(f"File exceeds the {settings.max_upload_mb} MB limit.")
    if not data:
        raise InvalidFileError("The file is empty.")

    parsed = file_parser.parse_upload(filename, data, sheet_name)
    if parsed.column_count == 0:
        raise InvalidFileError("No columns were found in the file.")

    stored = safe_filename(filename)
    keep_raw = parsed.file_type == "xlsx" and len(parsed.sheet_names) > 1
    if keep_raw or not settings.delete_uploads_after_processing:
        (settings.resolved_upload_dir / stored).write_bytes(data)
    else:
        stored = None

    display = filename.replace("\\", "/").rsplit("/", 1)[-1][:255]
    job = ImportJob(
        name=(name or display.rsplit(".", 1)[0])[:160],
        original_filename=display,
        stored_filename=stored,
        file_size=len(data),
        file_type=parsed.file_type,
        encoding=parsed.encoding,
        delimiter=parsed.delimiter,
        sheet_names=parsed.sheet_names,
        sheet_name=parsed.sheet_name,
        status="uploaded",
        step=2,
    )
    _store_parsed(job, parsed)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _store_parsed(job: ImportJob, parsed: file_parser.ParsedFile) -> None:
    job.columns = parsed.columns
    job.original_rows = parsed.rows
    job.row_count = parsed.row_count
    job.column_count = parsed.column_count
    job.inspection = file_parser.inspect(parsed)
    job.sheet_name = parsed.sheet_name


def select_sheet(db: Session, job: ImportJob, sheet_name: str) -> ImportJob:
    if job.file_type != "xlsx":
        raise InvalidStateError("Only XLSX imports have sheets.")
    path = _upload_path(job)
    if path is None or not path.exists():
        raise InvalidStateError(
            "The original file is no longer available; upload it again to switch sheet."
        )
    parsed = file_parser.parse_xlsx(path.read_bytes(), sheet_name)
    _store_parsed(job, parsed)
    # any previous mapping is invalid now
    job.mappings.clear()
    job.transformations.clear()
    _clear_results(job)
    job.schema_id = None
    job.status = "uploaded"
    job.step = 2
    db.commit()
    db.refresh(job)
    return job


def _clear_results(job: ImportJob) -> None:
    job.normalized_rows = []
    job.row_status = []
    job.overrides = {}
    job.edit_log = []
    job.summary = {}


# ----------------------------------------------------------------------------- schema + mapping
def _source_columns(job: ImportJob) -> list[SourceColumn]:
    out = []
    for col in job.columns:
        prof = job.inspection.get(col, {})
        out.append(SourceColumn(col, prof.get("detected_type", "string"), prof.get("examples", [])))
    return out


def _targets(job: ImportJob) -> list[TargetField]:
    return [TargetField.from_model(f) for f in job.schema.fields] if job.schema else []


def _memory_for(db: Session, schema_id: str) -> dict[str, str]:
    rows = db.scalars(select(MappingMemory).where(MappingMemory.schema_id == schema_id)).all()
    return {m.source_key: m.target_field for m in rows}


def detect_template(db: Session, job: ImportJob) -> tuple[MappingTemplate | None, float]:
    if not job.schema_id:
        return None, 0.0
    best, best_score = None, 0.0
    for t in db.scalars(
        select(MappingTemplate).where(MappingTemplate.schema_id == job.schema_id)
    ).all():
        score = template_match_score(t.source_columns, job.columns)
        if score > best_score:
            best, best_score = t, score
    if best and best_score >= TEMPLATE_MATCH_THRESHOLD:
        return best, best_score
    return None, best_score


def set_schema(db: Session, job: ImportJob, schema_id: str, ai_enabled: bool = False) -> ImportJob:
    schema = get_schema(db, schema_id)
    job.schema_id = schema.id
    job.schema = schema
    job.ai_enabled = bool(ai_enabled)
    job.template_id = None
    job.mappings.clear()
    job.transformations.clear()
    _clear_results(job)
    db.flush()
    suggest_mapping(db, job)
    job.status = "mapping"
    job.step = 3
    db.commit()
    db.refresh(job)
    return job


def suggest_mapping(db: Session, job: ImportJob) -> list[Mapping]:
    settings = get_settings()
    provider = build_provider(settings.gemini_api_key, settings.gemini_model)
    engine = MappingEngine(provider)
    memory = _memory_for(db, job.schema_id)
    suggestions = engine.suggest(_source_columns(job), _targets(job), memory, use_ai=job.ai_enabled)
    job.mappings.clear()
    for s in suggestions:
        job.mappings.append(
            Mapping(
                source_column=s.source_column,
                target_field=s.target_field,
                confidence=s.confidence,
                method=s.method,
                confirmed=s.confidence == "HIGH",
            )
        )
    db.flush()
    return job.mappings


def apply_template(db: Session, job: ImportJob, template_id: str) -> ImportJob:
    tpl = db.get(MappingTemplate, template_id)
    if tpl is None or tpl.schema_id != job.schema_id:
        raise NotFoundError("Mapping template not found for this schema.")
    by_key = {normalize_header(k): v for k, v in (tpl.mapping or {}).items()}
    for m in job.mappings:
        key = normalize_header(m.source_column)
        if key in by_key:
            m.target_field = by_key[key]
            m.ignored = by_key[key] is None
            m.confidence = "HIGH" if by_key[key] else "UNMAPPED"
            m.method = "template"
            m.confirmed = True
    job.template_id = tpl.id
    tpl.use_count += 1
    if tpl.transformations:
        job.transformations.clear()
        for i, t in enumerate(tpl.transformations):
            job.transformations.append(
                Transformation(
                    position=i,
                    kind=t["kind"],
                    target_field=t.get("target_field"),
                    params=t.get("params") or {},
                    enabled=t.get("enabled", True),
                    label=t.get("label", ""),
                )
            )
    db.commit()
    db.refresh(job)
    return job


def current_mapping(job: ImportJob) -> dict[str, str | None]:
    return {m.source_column: (None if m.ignored else m.target_field) for m in job.mappings}


def update_mapping(
    db: Session, job: ImportJob, mapping: dict[str, str | None], confirm: bool = True
) -> ImportJob:
    if not job.schema:
        raise InvalidStateError("Select a target schema before mapping.")
    targets = _targets(job)
    target_names = {t.name for t in targets}
    unknown_sources = [s for s in mapping if s not in job.columns]
    if unknown_sources:
        raise InvalidMappingError(
            "Unknown source columns.", details=[{"source_column": s} for s in unknown_sources]
        )
    unknown_targets = [t for t in mapping.values() if t and t not in target_names]
    if unknown_targets:
        raise InvalidMappingError(
            "Unknown target fields.", details=[{"target_field": t} for t in unknown_targets]
        )
    merged = current_mapping(job)
    merged.update(mapping)
    conflicts = find_conflicts(merged, targets)
    if conflicts:
        c = conflicts[0]
        raise InvalidMappingError(
            f"Target field '{c['target_field']}' is mapped more than once "
            f"({', '.join(c['sources'])}).",
            details=conflicts,
        )
    by_source = {m.source_column: m for m in job.mappings}
    for src, tgt in mapping.items():
        m = by_source.get(src)
        if m is None:
            m = Mapping(source_column=src)
            job.mappings.append(m)
        changed = (m.target_field != tgt) or (m.ignored != (tgt is None))
        m.target_field = tgt
        m.ignored = tgt is None
        if changed:
            m.method = "manual"
            m.confidence = "HIGH" if tgt else "UNMAPPED"
        m.confirmed = True
    if confirm:
        _remember_mappings(db, job)
        if not job.transformations:
            for i, spec in enumerate(
                suggest_transformations(job.schema.fields, current_mapping(job), job.inspection)
            ):
                job.transformations.append(
                    Transformation(
                        position=i,
                        kind=spec.kind,
                        target_field=spec.target_field,
                        params=spec.params,
                        enabled=spec.enabled,
                        suggested=True,
                        label=spec.label,
                    )
                )
        settings = get_settings()
        if settings.delete_uploads_after_processing:
            _delete_upload(job)
        job.status = "mapped"
        job.step = max(job.step, 4)
        _clear_results(job)
    db.commit()
    db.refresh(job)
    return job


def _remember_mappings(db: Session, job: ImportJob) -> None:
    for m in job.mappings:
        if not m.target_field or m.ignored:
            continue
        key = normalize_header(m.source_column)
        existing = db.scalar(
            select(MappingMemory).where(
                MappingMemory.schema_id == job.schema_id, MappingMemory.source_key == key
            )
        )
        if existing:
            if existing.target_field != m.target_field:
                existing.target_field = m.target_field
                existing.hits = 1
            else:
                existing.hits += 1
            existing.source_column = m.source_column
        else:
            db.add(
                MappingMemory(
                    schema_id=job.schema_id,
                    source_key=key,
                    source_column=m.source_column,
                    target_field=m.target_field,
                )
            )


def mapping_view(db: Session, job: ImportJob) -> list[dict]:
    out = []
    for m in job.mappings:
        prof = job.inspection.get(m.source_column, {})
        out.append(
            {
                "source_column": m.source_column,
                "target_field": None if m.ignored else m.target_field,
                "confidence": "UNMAPPED" if m.ignored else m.confidence,
                "method": m.method,
                "ignored": m.ignored,
                "confirmed": m.confirmed,
                "detected_type": prof.get("detected_type"),
                "examples": prof.get("examples", [])[:3],
                "empty_pct": prof.get("empty_pct"),
            }
        )
    return out


# ----------------------------------------------------------------------------- templates
def save_template(db: Session, job: ImportJob, name: str) -> MappingTemplate:
    if not job.schema_id:
        raise InvalidStateError("Select a schema first.")
    name = name.strip()
    if not name:
        raise InvalidInputError("Template name is required.")
    tpl = MappingTemplate(
        schema_id=job.schema_id,
        name=name[:120],
        source_columns=list(job.columns),
        mapping=current_mapping(job),
        transformations=[
            {
                "kind": t.kind,
                "target_field": t.target_field,
                "params": t.params,
                "enabled": t.enabled,
                "label": t.label,
            }
            for t in job.transformations
        ],
    )
    db.add(tpl)
    job.template_id = None
    db.commit()
    db.refresh(tpl)
    job.template_id = tpl.id
    db.commit()
    return tpl


def list_templates(db: Session, schema_id: str | None = None) -> list[MappingTemplate]:
    stmt = select(MappingTemplate).order_by(MappingTemplate.created_at.desc())
    if schema_id:
        stmt = stmt.where(MappingTemplate.schema_id == schema_id)
    return list(db.scalars(stmt).all())


def delete_template(db: Session, template_id: str) -> None:
    tpl = db.get(MappingTemplate, template_id)
    if tpl is None:
        raise NotFoundError("Mapping template not found.")
    db.delete(tpl)
    db.commit()


# ----------------------------------------------------------------------------- transformations
def set_transformations(db: Session, job: ImportJob, specs: list[dict]) -> ImportJob:
    if job.status in ("uploaded", "mapping"):
        raise InvalidStateError("Confirm the mapping before configuring transformations.")
    target_names = {f.name for f in job.schema.fields}
    job.transformations.clear()
    db.flush()
    for i, raw in enumerate(specs):
        kind = raw.get("kind")
        if kind not in KIND_LABELS:
            raise InvalidInputError(f"Unknown transformation kind '{kind}'.")
        tf = raw.get("target_field")
        if tf and tf not in target_names:
            raise InvalidInputError(f"Unknown target field '{tf}'.")
        job.transformations.append(
            Transformation(
                position=i,
                kind=kind,
                target_field=tf,
                params=raw.get("params") or {},
                enabled=bool(raw.get("enabled", True)),
                suggested=bool(raw.get("suggested", False)),
                label=raw.get("label") or KIND_LABELS[kind] + (f": {tf}" if tf else ""),
            )
        )
    _clear_results(job)
    job.status = "mapped"
    db.commit()
    db.refresh(job)
    return job


def transformations_view(job: ImportJob) -> list[dict]:
    return [
        {
            "id": t.id,
            "kind": t.kind,
            "target_field": t.target_field,
            "params": t.params or {},
            "enabled": t.enabled,
            "suggested": t.suggested,
            "label": t.label,
        }
        for t in job.transformations
    ]


# ----------------------------------------------------------------------------- run
def _engines(job: ImportJob) -> tuple[TransformationEngine, ValidationEngine]:
    mapping = current_mapping(job)
    specs = [TransformSpec.from_model(t) for t in job.transformations]
    tengine = TransformationEngine(job.columns, mapping, job.schema.fields, specs, job.defaults)
    tengine.infer_date_order(job.original_rows)
    structural_targets: set[str] = set()
    for s in specs:
        if s.enabled and s.kind == "combine" and s.target_field:
            structural_targets.add(s.target_field)
        if s.enabled and s.kind == "split":
            structural_targets.update(s.params.get("into", []))
        if s.enabled and s.kind == "default" and s.target_field:
            structural_targets.add(s.target_field)
    structural_targets.update(job.defaults.keys())
    vengine = ValidationEngine(
        job.schema.fields,
        mapping,
        job.schema.cross_field_rules or [],
        provided_fields=structural_targets,
    )
    return tengine, vengine


def _overrides_by_row(job: ImportJob) -> dict[int, dict[str, str | None]]:
    out: dict[int, dict[str, str | None]] = {}
    for key, value in (job.overrides or {}).items():
        r, f = key.split(":", 1)
        out.setdefault(int(r), {})[f] = value
    return out


def run_validation(db: Session, job: ImportJob) -> ImportJob:
    if not job.schema or job.status in ("uploaded", "mapping"):
        raise InvalidStateError("Confirm the mapping before validating.")
    started = time.perf_counter()
    tengine, vengine = _engines(job)
    result = tengine.run(job.original_rows, _overrides_by_row(job))
    vengine.notes = result.notes
    originals = original_records(job)
    vres = vengine.run(result.rows, originals)
    job.normalized_rows = result.rows
    job.row_status = vres.row_status
    _replace_issues(db, job, vres.issues)
    summary = _summary(job, vres.issues, vres.row_status)
    summary["validation_ms"] = round((time.perf_counter() - started) * 1000)
    row_notes: dict[str, dict[str, str]] = {}
    for (i, f), code in result.notes.items():
        row_notes.setdefault(str(i), {})[f] = code
    summary["row_notes"] = row_notes
    job.summary = summary
    job.status = "validated"
    job.step = max(job.step, 5)
    db.commit()
    db.refresh(job)
    return job


def original_records(job: ImportJob) -> list[dict]:
    """Original (untouched) source value per target field, for Original/Normalized view."""
    mapping = current_mapping(job)
    idx = {c: i for i, c in enumerate(job.columns)}
    first_source: dict[str, int] = {}
    for src, tgt in mapping.items():
        if tgt and tgt not in first_source:
            first_source[tgt] = idx[src]
    out = []
    for row in job.original_rows:
        out.append({tgt: (row[i] if i < len(row) else None) for tgt, i in first_source.items()})
    return out


def _replace_issues(db: Session, job: ImportJob, issues: list[Issue]) -> None:
    db.execute(delete(ValidationIssue).where(ValidationIssue.job_id == job.id))
    if issues:
        db.execute(
            ValidationIssue.__table__.insert(),
            [
                {
                    "job_id": job.id,
                    "row_index": i.row_index,
                    "field": i.field,
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "original_value": i.original_value,
                }
                for i in issues
            ],
        )


def _summary(job: ImportJob, issues: list[Issue], statuses: list[str]) -> dict:
    counts = Counter(statuses)
    total = len(statuses)
    ready = counts.get("ready", 0) + counts.get("warning", 0)
    sev = Counter(i.severity for i in issues)
    return {
        "rows": total,
        "ready": counts.get("ready", 0),
        "warning": counts.get("warning", 0),
        "error": counts.get("error", 0),
        "importable": ready,
        "ready_pct": round(100 * ready / total, 1) if total else 0.0,
        "issue_counts": {
            "ERROR": sev.get("ERROR", 0),
            "WARNING": sev.get("WARNING", 0),
            "INFO": sev.get("INFO", 0),
        },
        "top_issues": top_issues(issues),
        "dataset_issues": [i.as_dict() for i in issues if i.row_index is None],
    }


def refresh_summary(db: Session, job: ImportJob) -> None:
    issues = load_issues(db, job)
    job.summary = {**job.summary, **_summary(job, issues, job.row_status)}


def load_issues(db: Session, job: ImportJob) -> list[Issue]:
    rows = db.scalars(select(ValidationIssue).where(ValidationIssue.job_id == job.id)).all()
    return [
        Issue(r.row_index, r.field, r.severity, r.code, r.message, r.original_value) for r in rows
    ]


# ----------------------------------------------------------------------------- row-level edits
def _revalidate_rows(db: Session, job: ImportJob, row_indexes: set[int]) -> None:
    """Recompute transformation + validation for a set of rows only.

    Uniqueness is global, so duplicate sets are recomputed over the whole dataset
    (an O(n) Counter) and the rows that gained or lost a duplicate are refreshed too.
    JSON columns are replaced with new list objects (never mutated in place) so that
    SQLAlchemy's change detection sees the update.
    """
    tengine, vengine = _engines(job)
    rows = list(job.normalized_rows)
    statuses = list(job.row_status)
    row_notes = dict(job.summary.get("row_notes", {}))
    originals = original_records(job)
    before_dups = vengine.duplicate_index(rows)
    affected = set(row_indexes)
    by_row = _overrides_by_row(job)
    for i in row_indexes:
        record, notes = tengine.transform_row(job.original_rows[i], by_row.get(i))
        rows[i] = record
        if notes:
            row_notes[str(i)] = notes
        else:
            row_notes.pop(str(i), None)
    after_dups = vengine.duplicate_index(rows)
    for fname, values in after_dups.items():
        changed_values = values ^ before_dups.get(fname, set())
        if changed_values:
            for j, rec in enumerate(rows):
                if rec.get(fname) in changed_values:
                    affected.add(j)
    for i in affected:
        notes_i = row_notes.get(str(i), {})
        issues = vengine.validate_row(i, rows[i], after_dups, originals[i])
        for f, code in notes_i.items():
            sev, msg = TYPE_ISSUES.get(code, ("WARNING", code))
            issues.append(Issue(i, f, sev, code, msg, rows[i].get(f)))
        db.execute(
            delete(ValidationIssue).where(
                ValidationIssue.job_id == job.id, ValidationIssue.row_index == i
            )
        )
        if issues:
            db.execute(
                ValidationIssue.__table__.insert(),
                [
                    {
                        "job_id": job.id,
                        "row_index": x.row_index,
                        "field": x.field,
                        "severity": x.severity,
                        "code": x.code,
                        "message": x.message,
                        "original_value": x.original_value,
                    }
                    for x in issues
                ],
            )
        statuses[i] = ValidationEngine.row_statuses(issues, i + 1)[i]
    job.normalized_rows = rows
    job.row_status = statuses
    job.summary = {**job.summary, "row_notes": row_notes}
    refresh_summary(db, job)


def edit_cell(db: Session, job: ImportJob, row_index: int, field: str, value: str | None) -> dict:
    _require_validated(job)
    if row_index < 0 or row_index >= job.row_count:
        raise NotFoundError(f"Row {row_index} does not exist.")
    if field not in {f.name for f in job.schema.fields}:
        raise NotFoundError(f"Field '{field}' is not part of the schema.")
    value = None if value in (None, "") else str(value)
    before = job.normalized_rows[row_index].get(field)
    overrides = dict(job.overrides or {})
    overrides[f"{row_index}:{field}"] = value
    job.overrides = overrides
    job.edit_log = [
        *job.edit_log,
        {
            "label": f"Edit row {row_index + 2} · {field}",
            "changes": [{"row": row_index, "field": field, "before": before, "after": value}],
        },
    ]
    _revalidate_rows(db, job, {row_index})
    db.commit()
    db.refresh(job)
    return row_view(db, job, row_index)


def bulk_replace(
    db: Session,
    job: ImportJob,
    field: str,
    from_value: str | None,
    to_value: str | None,
    match_original: bool = False,
) -> dict:
    """Set ``to_value`` for every row whose current (or original) value equals ``from_value``."""
    _require_validated(job)
    if field not in {f.name for f in job.schema.fields}:
        raise NotFoundError(f"Field '{field}' is not part of the schema.")
    to_value = None if to_value in (None, "") else str(to_value)
    originals = original_records(job) if match_original else None
    changes = []
    overrides = dict(job.overrides or {})
    for i, rec in enumerate(job.normalized_rows):
        current = rec.get(field)
        probe = originals[i].get(field) if originals else current
        if _eq(probe, from_value):
            changes.append({"row": i, "field": field, "before": current, "after": to_value})
            overrides[f"{i}:{field}"] = to_value
    if not changes:
        return {"changed": 0}
    job.overrides = overrides
    job.edit_log = [
        *job.edit_log,
        {
            "label": f"Replace {from_value!r} → {to_value!r} in {field} ({len(changes)} rows)",
            "changes": changes,
        },
    ]
    _revalidate_rows(db, job, {c["row"] for c in changes})
    db.commit()
    db.refresh(job)
    return {"changed": len(changes), "summary": job.summary}


def bulk_trim(db: Session, job: ImportJob, field: str) -> dict:
    _require_validated(job)
    changes = []
    overrides = dict(job.overrides or {})
    for i, rec in enumerate(job.normalized_rows):
        v = rec.get(field)
        if isinstance(v, str) and v != norm.trim(v):
            changes.append({"row": i, "field": field, "before": v, "after": norm.trim(v)})
            overrides[f"{i}:{field}"] = norm.trim(v)
    if not changes:
        return {"changed": 0}
    job.overrides = overrides
    job.edit_log = [
        *job.edit_log,
        {"label": f"Trim {len(changes)} values in {field}", "changes": changes},
    ]
    _revalidate_rows(db, job, {c["row"] for c in changes})
    db.commit()
    db.refresh(job)
    return {"changed": len(changes), "summary": job.summary}


def undo_last(db: Session, job: ImportJob) -> dict:
    _require_validated(job)
    if not job.edit_log:
        return {"undone": None}
    log_entries = list(job.edit_log)
    last = log_entries.pop()
    rows = {ch["row"] for ch in last["changes"]}
    # Rebuild overrides from the remaining log so an earlier edit of the same cell
    # is restored instead of dropped.
    job.overrides = _overrides_from_log(log_entries)
    job.edit_log = log_entries
    _revalidate_rows(db, job, rows)
    db.commit()
    db.refresh(job)
    return {"undone": last["label"], "summary": job.summary}


def _overrides_from_log(log_entries: list[dict]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for entry in log_entries:
        for ch in entry["changes"]:
            out[f"{ch['row']}:{ch['field']}"] = ch.get("after")
    return out


def reset_column(db: Session, job: ImportJob, field: str) -> dict:
    """Drop every manual override of a column (back to transformed/original values)."""
    _require_validated(job)
    overrides = dict(job.overrides or {})
    rows = set()
    for key in list(overrides):
        r, f = key.split(":", 1)
        if f == field:
            overrides.pop(key)
            rows.add(int(r))
    job.overrides = overrides
    job.edit_log = [e for e in job.edit_log if not all(c["field"] == field for c in e["changes"])]
    if rows:
        _revalidate_rows(db, job, rows)
    db.commit()
    db.refresh(job)
    return {"reset": len(rows), "summary": job.summary}


def _eq(a, b) -> bool:
    return (a or "") == (b or "")


def _require_validated(job: ImportJob) -> None:
    if job.status not in ("validated", "completed") or not job.normalized_rows and job.row_count:
        raise InvalidStateError("Run validation before editing rows.")


# ----------------------------------------------------------------------------- bulk fix suggestions
def bulk_fix_suggestions(db: Session, job: ImportJob, limit: int = 8) -> list[dict]:
    if job.status not in ("validated", "completed"):
        return []
    issues = load_issues(db, job)
    suggestions: list[dict] = []
    by_field_code: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for iss in issues:
        if iss.row_index is None or not iss.field:
            continue
        value = job.normalized_rows[iss.row_index].get(iss.field)
        if value is None:
            continue
        by_field_code[(iss.field, iss.code)][value] += 1
    for (fname, code), counter in by_field_code.items():
        for value, count in counter.most_common(5):
            if code == "country_unknown":
                fix = norm.country_suggestion(value)
                if fix:
                    suggestions.append(
                        _sug(fname, value, fix, count, f'Normalize "{value}" → "{fix}" in {fname}')
                    )
            elif code == "currency_unknown":
                fix = norm.normalize_currency(value.upper()).value if len(value) == 3 else None
                if fix and fix != value:
                    suggestions.append(
                        _sug(fname, value, fix, count, f'Normalize "{value}" → "{fix}" in {fname}')
                    )
            elif code == "boolean_invalid" and value.lower() in (
                "yes",
                "no",
                "y",
                "n",
                "true",
                "false",
                "1",
                "0",
            ):
                fix = norm.normalize_boolean(value).value
                suggestions.append(
                    _sug(fname, value, fix, count, f'Normalize "{value}" → "{fix}" in {fname}')
                )
            elif code == "email_invalid" and " " in value.strip():
                fix = value.replace(" ", "")
                if norm.normalize_email(fix).ok:
                    suggestions.append(
                        _sug(fname, value, fix, count, f'Remove spaces in "{value}"')
                    )
            elif code == "enum":
                allowed = (
                    next((f.rules.get("enum") for f in job.schema.fields if f.name == fname), None)
                    or []
                )
                match = next((a for a in allowed if str(a).lower() == value.lower()), None)
                if match:
                    suggestions.append(
                        _sug(
                            fname,
                            value,
                            match,
                            count,
                            f'Normalize "{value}" → "{match}" in {fname}',
                        )
                    )
    # whitespace that survived (trim disabled)
    for f in job.schema.fields:
        n = sum(
            1
            for rec in job.normalized_rows
            if isinstance(rec.get(f.name), str) and rec[f.name] != norm.trim(rec[f.name])
        )
        if n:
            suggestions.append(
                {
                    "type": "trim",
                    "field": f.name,
                    "count": n,
                    "label": f"Trim leading/trailing spaces in {n} values of {f.name}",
                }
            )
    suggestions.sort(key=lambda s: -s["count"])
    return suggestions[:limit]


def _sug(field: str, from_value: str, to_value: str, count: int, label: str) -> dict:
    return {
        "type": "replace",
        "field": field,
        "from_value": from_value,
        "to_value": to_value,
        "count": count,
        "label": label,
    }


# ----------------------------------------------------------------------------- views
def row_view(db: Session, job: ImportJob, row_index: int) -> dict:
    issues = db.scalars(
        select(ValidationIssue).where(
            ValidationIssue.job_id == job.id, ValidationIssue.row_index == row_index
        )
    ).all()
    return _row_dict(
        job,
        row_index,
        original_records(job)[row_index],
        [(i.field, i.severity, i.code, i.message) for i in issues],
    )


def _row_dict(job: ImportJob, i: int, original: dict, issues: list[tuple]) -> dict:
    cells = {}
    by_field: dict[str, list] = defaultdict(list)
    for f, sev, code, msg in issues:
        by_field[f or ""].append({"severity": sev, "code": code, "message": msg})
    for f in job.schema.fields:
        cells[f.name] = {
            "value": job.normalized_rows[i].get(f.name) if i < len(job.normalized_rows) else None,
            "original": original.get(f.name),
            "issues": by_field.get(f.name, []),
            "edited": f"{i}:{f.name}" in (job.overrides or {}),
        }
    return {
        "index": i,
        "row_number": i + 2,
        "status": job.row_status[i] if i < len(job.row_status) else "ready",
        "cells": cells,
    }


def rows_page(
    db: Session,
    job: ImportJob,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
    field: str | None = None,
    code: str | None = None,
    search: str | None = None,
) -> dict:
    _require_validated(job)
    indexes = list(range(job.row_count))
    if status in ("ready", "warning", "error"):
        indexes = [i for i in indexes if job.row_status[i] == status]
    elif status == "issues":
        indexes = [i for i in indexes if job.row_status[i] != "ready"]
    if field or code:
        stmt = select(ValidationIssue.row_index).where(
            ValidationIssue.job_id == job.id, ValidationIssue.row_index.is_not(None)
        )
        if field:
            stmt = stmt.where(ValidationIssue.field == field)
        if code:
            stmt = stmt.where(ValidationIssue.code == code)
        wanted = set(db.scalars(stmt.distinct()).all())
        indexes = [i for i in indexes if i in wanted]
    if search:
        q = search.lower()
        originals_all = original_records(job)
        indexes = [
            i
            for i in indexes
            if any(q in str(v).lower() for v in job.normalized_rows[i].values() if v is not None)
            or any(q in str(v).lower() for v in originals_all[i].values() if v is not None)
        ]
    total = len(indexes)
    page = max(1, page)
    start = (page - 1) * page_size
    chunk = indexes[start : start + page_size]
    originals = original_records(job)
    issues_by_row: dict[int, list[tuple]] = defaultdict(list)
    if chunk:
        rows = db.scalars(
            select(ValidationIssue).where(
                ValidationIssue.job_id == job.id, ValidationIssue.row_index.in_(chunk)
            )
        ).all()
        for r in rows:
            issues_by_row[r.row_index].append((r.field, r.severity, r.code, r.message))
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, -(-total // page_size)),
        "rows": [_row_dict(job, i, originals[i], issues_by_row.get(i, [])) for i in chunk],
    }


def issues_page(
    db: Session,
    job: ImportJob,
    page: int = 1,
    page_size: int = 100,
    severity: str | None = None,
    field: str | None = None,
    code: str | None = None,
) -> dict:
    stmt = select(ValidationIssue).where(ValidationIssue.job_id == job.id)
    count_stmt = (
        select(func.count()).select_from(ValidationIssue).where(ValidationIssue.job_id == job.id)
    )
    if severity:
        stmt = stmt.where(ValidationIssue.severity == severity.upper())
        count_stmt = count_stmt.where(ValidationIssue.severity == severity.upper())
    if field:
        stmt = stmt.where(ValidationIssue.field == field)
        count_stmt = count_stmt.where(ValidationIssue.field == field)
    if code:
        stmt = stmt.where(ValidationIssue.code == code)
        count_stmt = count_stmt.where(ValidationIssue.code == code)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(ValidationIssue.row_index, ValidationIssue.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [
            {
                "row_index": r.row_index,
                "row_number": (r.row_index + 2) if r.row_index is not None else None,
                "field": r.field,
                "severity": r.severity,
                "code": r.code,
                "message": r.message,
                "original_value": r.original_value,
            }
            for r in rows
        ],
    }


def job_to_dict(job: ImportJob, include_rows: bool = False) -> dict:
    d = {
        "id": job.id,
        "name": job.name,
        "original_filename": job.original_filename,
        "file_type": job.file_type,
        "file_size": job.file_size,
        "encoding": job.encoding,
        "delimiter": job.delimiter,
        "sheet_names": job.sheet_names or [],
        "sheet_name": job.sheet_name,
        "status": job.status,
        "step": job.step,
        "schema_id": job.schema_id,
        "schema_name": job.schema.name if job.schema else None,
        "template_id": job.template_id,
        "ai_enabled": job.ai_enabled,
        "row_count": job.row_count,
        "column_count": job.column_count,
        "columns": job.columns,
        "inspection": job.inspection,
        "summary": {k: v for k, v in (job.summary or {}).items() if k != "row_notes"},
        "defaults": job.defaults or {},
        "edit_log": [e["label"] for e in (job.edit_log or [])],
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if include_rows:
        d["normalized_rows"] = job.normalized_rows
        d["row_status"] = job.row_status
    return d


def set_defaults(db: Session, job: ImportJob, defaults: dict[str, str]) -> ImportJob:
    names = {f.name for f in job.schema.fields} if job.schema else set()
    bad = [k for k in defaults if k not in names]
    if bad:
        raise InvalidInputError("Unknown fields in defaults.", details=[{"field": b} for b in bad])
    job.defaults = {k: v for k, v in defaults.items() if v not in (None, "")}
    _clear_results(job)
    if job.status in ("validated", "completed"):
        job.status = "mapped"
    db.commit()
    db.refresh(job)
    return job


def mark_completed(db: Session, job: ImportJob) -> ImportJob:
    _require_validated(job)
    job.status = "completed"
    job.step = 7
    job.completed_at = datetime.now(UTC)
    db.commit()
    db.refresh(job)
    return job


def issue_title(code: str) -> str:
    return ISSUE_TITLES.get(code, code)


def purge_old_uploads_for_jobs(db: Session, hours: int | None = None) -> int:
    settings = get_settings()
    hours = settings.upload_retention_hours if hours is None else hours
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    n = 0
    for job in db.scalars(
        select(ImportJob).where(
            ImportJob.stored_filename.is_not(None), ImportJob.created_at < cutoff
        )
    ).all():
        _delete_upload(job)
        n += 1
    db.commit()
    return n
