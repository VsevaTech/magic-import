"""Pydantic request / response models for the REST API (v1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["HIGH", "MEDIUM", "LOW", "UNMAPPED"]
Severity = Literal["ERROR", "WARNING", "INFO"]
FieldType = Literal[
    "string",
    "integer",
    "decimal",
    "boolean",
    "email",
    "phone",
    "date",
    "datetime",
    "country",
    "currency",
    "url",
    "enum",
]


# ----------------------------------------------------------------------------- errors
class ErrorBody(BaseModel):
    code: str = Field(examples=["invalid_mapping"])
    message: str = Field(examples=["Target field 'email' is mapped more than once"])
    details: list[Any] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Unified error contract used by every endpoint."""

    error: ErrorBody

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "invalid_mapping",
                        "message": "Target field 'email' is mapped more than once",
                        "details": [{"target_field": "email", "sources": ["Mail", "E-mail"]}],
                    }
                }
            ]
        }
    )


ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid file"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Invalid state"},
    413: {"model": ErrorResponse, "description": "File too large"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


# ----------------------------------------------------------------------------- schemas
class SchemaFieldIn(BaseModel):
    name: str = Field(examples=["email"], pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=80)
    display_name: str | None = Field(default=None, examples=["Email"])
    type: FieldType = "string"
    required: bool = False
    unique: bool = False
    description: str = ""
    aliases: list[str] = Field(default_factory=list, examples=[["mail", "e-mail address"]])
    example: str = ""
    default: str | None = None
    rules: dict[str, Any] = Field(
        default_factory=dict,
        description="Validation rules: min_length, max_length, regex, enum, min, max, "
        "min_date, max_date.",
        examples=[{"max_length": 120}],
    )
    allow_multiple_sources: bool = False


class SchemaIn(BaseModel):
    name: str = Field(examples=["Customer Import v1"], min_length=1, max_length=120)
    description: str = ""
    fields: list[SchemaFieldIn] = Field(min_length=1)
    cross_field_rules: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Declarative cross-field rules, e.g. "
        '{"when": {"field": "country", "op": "eq", "value": "IL"}, '
        '"then": {"field": "phone", "op": "phone_region", "value": "IL"}, '
        '"severity": "WARNING"}',
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Customer Import v1",
                    "description": "Customers for the CRM",
                    "fields": [
                        {"name": "customer_name", "type": "string", "required": True},
                        {
                            "name": "email",
                            "type": "email",
                            "required": True,
                            "unique": True,
                            "aliases": ["mail", "e-mail address"],
                        },
                        {"name": "phone", "type": "phone"},
                        {"name": "country", "type": "country", "default": "IL"},
                    ],
                }
            ]
        }
    )


class SchemaUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    fields: list[SchemaFieldIn] | None = None
    cross_field_rules: list[dict[str, Any]] | None = None


class SchemaFieldOut(SchemaFieldIn):
    display_name: str = ""


class SchemaOut(BaseModel):
    id: str
    name: str
    description: str
    version: int
    is_builtin: bool
    cross_field_rules: list[dict[str, Any]]
    created_at: str | None
    fields: list[SchemaFieldOut]


# ----------------------------------------------------------------------------- imports
class ColumnProfile(BaseModel):
    index: int
    empty_pct: float
    unique_pct: float
    distinct_count: int
    non_empty_count: int
    is_empty: bool
    is_duplicate_header: bool
    detected_type: str
    examples: list[str]


class ImportOut(BaseModel):
    id: str
    name: str
    original_filename: str
    file_type: str
    file_size: int
    encoding: str
    delimiter: str | None
    sheet_names: list[str]
    sheet_name: str | None
    status: str = Field(
        description="uploaded → mapping → mapped → validated → completed",
        examples=["validated"],
    )
    step: int
    schema_id: str | None
    schema_name: str | None
    template_id: str | None
    ai_enabled: bool
    row_count: int
    column_count: int
    columns: list[str]
    inspection: dict[str, ColumnProfile]
    summary: dict[str, Any]
    defaults: dict[str, str]
    edit_log: list[str]
    created_at: str | None
    updated_at: str | None
    completed_at: str | None


class ImportListOut(BaseModel):
    items: list[ImportOut]


class SelectSchemaIn(BaseModel):
    schema_id: str
    ai_enabled: bool = Field(
        default=False,
        description="Allow Gemini to suggest mappings for columns the deterministic "
        "engine could not resolve. Only column names + masked samples are sent.",
    )


class SelectSheetIn(BaseModel):
    sheet_name: str


class MappingRow(BaseModel):
    source_column: str
    target_field: str | None
    confidence: Confidence
    method: str = Field(
        description="saved | exact | normalized | alias | fuzzy | type | ai | template | manual"
    )
    ignored: bool
    confirmed: bool
    detected_type: str | None = None
    examples: list[str] = Field(default_factory=list)
    empty_pct: float | None = None


class MappingOut(BaseModel):
    mapping: list[MappingRow]
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    template_suggestion: dict[str, Any] | None = None


class MappingIn(BaseModel):
    mapping: dict[str, str | None] = Field(
        description="source column → target field (null = ignore column)",
        examples=[{"E-mail Address": "email", "Notes": None}],
    )
    confirm: bool = Field(default=True, description="Confirm and remember the mapping.")


class TransformationIn(BaseModel):
    kind: str = Field(examples=["phone"])
    target_field: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    suggested: bool = False
    label: str | None = None


class TransformationsIn(BaseModel):
    transformations: list[TransformationIn]


class TransformationOut(TransformationIn):
    id: str | None = None
    label: str = ""


class DefaultsIn(BaseModel):
    defaults: dict[str, str] = Field(examples=[{"country": "IL", "status": "active"}])


class IssueOut(BaseModel):
    row_index: int | None
    row_number: int | None
    field: str | None
    severity: Severity
    code: str
    message: str
    original_value: str | None


class IssuesOut(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[IssueOut]


class CellOut(BaseModel):
    value: str | None
    original: str | None
    issues: list[dict[str, str]]
    edited: bool


class RowOut(BaseModel):
    index: int
    row_number: int
    status: Literal["ready", "warning", "error"]
    cells: dict[str, CellOut]


class RowsOut(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int
    rows: list[RowOut]


class CellEditIn(BaseModel):
    field: str
    value: str | None


class BulkReplaceIn(BaseModel):
    field: str
    from_value: str | None
    to_value: str | None
    match_original: bool = False


class BulkTrimIn(BaseModel):
    field: str


class ResetColumnIn(BaseModel):
    field: str


class TemplateIn(BaseModel):
    name: str = Field(examples=["Acme CRM Customer Export"], min_length=1)


class TemplateOut(BaseModel):
    id: str
    schema_id: str
    name: str
    source_columns: list[str]
    mapping: dict[str, str | None]
    use_count: int
    created_at: str | None


class ApplyTemplateIn(BaseModel):
    template_id: str


class OkOut(BaseModel):
    ok: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)
