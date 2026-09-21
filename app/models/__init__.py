"""SQLAlchemy ORM models.

The model is single-workspace today, but every aggregate root carries a nullable
``workspace_id`` so a workspace/user layer can be added later without migrations
that rewrite the data model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class ImportSchema(Base):
    """Target data contract (e.g. "Customer Import v1")."""

    __tablename__ = "schemas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    cross_field_rules: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    fields: Mapped[list[SchemaField]] = relationship(
        back_populates="schema",
        cascade="all, delete-orphan",
        order_by="SchemaField.position",
        lazy="selectin",
    )
    imports: Mapped[list[ImportJob]] = relationship(back_populates="schema")


class SchemaField(Base):
    __tablename__ = "schema_fields"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    schema_id: Mapped[str] = mapped_column(ForeignKey("schemas.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    type: Mapped[str] = mapped_column(String(20), default="string")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    unique: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    example: Mapped[str] = mapped_column(String(200), default="")
    default: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # {"min_length":..,"max_length":..,"regex":..,"enum":[..],"min":..,"max":..,
    #  "min_date":..,"max_date":..}
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    allow_multiple_sources: Mapped[bool] = mapped_column(Boolean, default=False)

    schema: Mapped[ImportSchema] = relationship(back_populates="fields")


class ImportJob(Base):
    """One uploaded file being turned into the target shape."""

    __tablename__ = "imports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    stored_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_type: Mapped[str] = mapped_column(String(10), default="")  # csv | xlsx
    encoding: Mapped[str] = mapped_column(String(40), default="")
    delimiter: Mapped[str | None] = mapped_column(String(4), nullable=True)
    sheet_names: Mapped[list] = mapped_column(JSON, default=list)
    sheet_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Wizard state: uploaded -> inspected -> mapped -> transformed -> validated -> completed
    status: Mapped[str] = mapped_column(String(20), default="uploaded", index=True)
    step: Mapped[int] = mapped_column(Integer, default=1)

    schema_id: Mapped[str | None] = mapped_column(
        ForeignKey("schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Parsed data. Original values are NEVER mutated during an import job.
    columns: Mapped[list] = mapped_column(JSON, default=list)  # source headers
    original_rows: Mapped[list] = mapped_column(JSON, default=list)  # list[list[str|None]]
    inspection: Mapped[dict] = mapped_column(JSON, default=dict)  # per-column profile
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)

    # Results of transform + validate: list[dict[target_field -> value]]
    normalized_rows: Mapped[list] = mapped_column(JSON, default=list)
    # Per-row status: "ready" | "warning" | "error"
    row_status: Mapped[list] = mapped_column(JSON, default=list)
    # Manual edits: {"row:field": value}; applied on top of transformations
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    # Undo stack of edit batches [{"label":..,"changes":[{"row":..,"field":..,"before":..}]}]
    edit_log: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    defaults: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    schema: Mapped[ImportSchema | None] = relationship(back_populates="imports", lazy="selectin")
    mappings: Mapped[list[Mapping]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    transformations: Mapped[list[Transformation]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Transformation.position",
        lazy="selectin",
    )
    issues: Mapped[list[ValidationIssue]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="noload"
    )


class Mapping(Base):
    """source column -> target field for one job."""

    __tablename__ = "mappings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), default="UNMAPPED")
    method: Mapped[str] = mapped_column(String(20), default="")  # saved|exact|normalized|...
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    job: Mapped[ImportJob] = relationship(back_populates="mappings")


class Transformation(Base):
    """A value transformation applied to a target field (or a virtual field)."""

    __tablename__ = "transformations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    suggested: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(200), default="")

    job: Mapped[ImportJob] = relationship(back_populates="transformations")


class ValidationIssue(Base):
    __tablename__ = "validation_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("imports.id", ondelete="CASCADE"), index=True)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    field: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[ImportJob] = relationship(back_populates="issues")


class MappingTemplate(Base):
    """A saved header-signature -> mapping, e.g. "Acme CRM Customer Export"."""

    __tablename__ = "mapping_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    schema_id: Mapped[str] = mapped_column(ForeignKey("schemas.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_columns: Mapped[list] = mapped_column(JSON, default=list)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)  # source -> target|None
    transformations: Mapped[list] = mapped_column(JSON, default=list)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MappingMemory(Base):
    """Confirmed (source header -> target field) pairs remembered across imports."""

    __tablename__ = "mapping_memory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    schema_id: Mapped[str] = mapped_column(ForeignKey("schemas.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[str] = mapped_column(String(255), index=True)  # normalized header
    source_column: Mapped[str] = mapped_column(String(255))
    target_field: Mapped[str] = mapped_column(String(80))
    hits: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


__all__ = [
    "ImportJob",
    "ImportSchema",
    "Mapping",
    "MappingMemory",
    "MappingTemplate",
    "SchemaField",
    "Transformation",
    "ValidationIssue",
]
