"""Schema CRUD + built-in schema seeding."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportSchema, SchemaField
from app.services.errors import InvalidInputError, NotFoundError

FIELD_TYPES = [
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

BUILTIN_SCHEMAS: list[dict] = [
    {
        "name": "Customer Import v1",
        "description": "Customers / CRM contacts for the target system.",
        "cross_field_rules": [
            {
                "when": {"field": "country", "op": "eq", "value": "IL"},
                "then": {"field": "phone", "op": "phone_region", "value": "IL"},
                "severity": "WARNING",
                "message": "Phone does not look like an Israeli number although country is IL",
            }
        ],
        "fields": [
            {
                "name": "customer_id",
                "display_name": "Customer ID",
                "type": "string",
                "unique": True,
                "description": "External identifier of the customer (unique).",
                "aliases": [
                    "id",
                    "customer ref",
                    "customer reference",
                    "ref",
                    "client id",
                    "cust id",
                    "customer no",
                    "customer number",
                ],
                "example": "C-1001",
            },
            {
                "name": "customer_name",
                "display_name": "Customer name",
                "type": "string",
                "required": True,
                "description": "Full name of the customer or contact person.",
                "aliases": [
                    "name",
                    "client",
                    "full name",
                    "customer",
                    "contact",
                    "contact name",
                    "client name",
                    "person",
                ],
                "example": "Dana Levi",
                "rules": {"min_length": 2, "max_length": 120},
            },
            {
                "name": "email",
                "display_name": "Email",
                "type": "email",
                "required": True,
                "unique": True,
                "description": "Primary contact email address.",
                "aliases": [
                    "mail",
                    "e-mail",
                    "email address",
                    "e-mail address",
                    "contact email",
                    "mail address",
                    "cust mail addr",
                ],
                "example": "dana@example.com",
            },
            {
                "name": "phone",
                "display_name": "Phone",
                "type": "phone",
                "description": "Contact phone number in E.164 format.",
                "aliases": [
                    "mobile",
                    "mobile no",
                    "telephone",
                    "tel",
                    "phone number",
                    "cell",
                    "mobile number",
                    "contact number",
                ],
                "example": "+972501234567",
            },
            {
                "name": "company",
                "display_name": "Company",
                "type": "string",
                "description": "Company / organization the customer belongs to.",
                "aliases": [
                    "organization",
                    "organisation",
                    "business",
                    "company name",
                    "employer",
                    "firm",
                    "account",
                ],
                "example": "Acme Ltd",
            },
            {
                "name": "country",
                "display_name": "Country",
                "type": "country",
                "description": "Country as ISO 3166-1 alpha-2 code.",
                "aliases": ["country name", "country code", "location", "nation", "region"],
                "example": "IL",
            },
            {
                "name": "created_at",
                "display_name": "Created at",
                "type": "date",
                "description": "Date the customer record was created.",
                "aliases": [
                    "created",
                    "joined",
                    "signup date",
                    "registration date",
                    "date created",
                    "created date",
                    "since",
                ],
                "example": "2026-09-21",
            },
        ],
    },
    {
        "name": "Merchant Import",
        "description": "Merchants onboarded to the payments platform.",
        "fields": [
            {
                "name": "merchant_id",
                "display_name": "Merchant ID",
                "type": "string",
                "required": True,
                "unique": True,
                "aliases": ["mid", "merchant ref", "id"],
                "description": "Unique merchant identifier.",
            },
            {
                "name": "legal_name",
                "display_name": "Legal name",
                "type": "string",
                "required": True,
                "aliases": ["company", "legal entity", "business name"],
                "description": "Registered legal name.",
            },
            {
                "name": "trading_name",
                "display_name": "Trading name",
                "type": "string",
                "aliases": ["dba", "brand", "store name"],
                "description": "Doing-business-as name.",
            },
            {
                "name": "email",
                "display_name": "Email",
                "type": "email",
                "required": True,
                "aliases": ["mail", "contact email"],
                "description": "Merchant contact email.",
            },
            {
                "name": "phone",
                "display_name": "Phone",
                "type": "phone",
                "aliases": ["telephone", "mobile"],
                "description": "Merchant contact phone.",
            },
            {
                "name": "country",
                "display_name": "Country",
                "type": "country",
                "required": True,
                "aliases": ["country code", "location"],
                "description": "ISO 3166-1 alpha-2.",
            },
            {
                "name": "mcc",
                "display_name": "MCC",
                "type": "string",
                "aliases": ["merchant category code", "category code"],
                "description": "Merchant category code.",
                "rules": {"regex": r"\d{4}"},
            },
            {
                "name": "status",
                "display_name": "Status",
                "type": "enum",
                "aliases": ["state"],
                "description": "Onboarding status.",
                "rules": {"enum": ["active", "pending", "blocked"]},
                "default": "pending",
            },
            {
                "name": "settlement_currency",
                "display_name": "Settlement currency",
                "type": "currency",
                "aliases": ["currency", "ccy"],
                "description": "ISO 4217 currency.",
            },
            {
                "name": "onboarded_at",
                "display_name": "Onboarded at",
                "type": "date",
                "aliases": ["created", "joined", "start date"],
                "description": "Onboarding date.",
            },
        ],
    },
    {
        "name": "Product Import",
        "description": "Catalog products with prices.",
        "fields": [
            {
                "name": "sku",
                "display_name": "SKU",
                "type": "string",
                "required": True,
                "unique": True,
                "aliases": ["code", "product code", "article", "item code"],
                "description": "Stock keeping unit.",
            },
            {
                "name": "name",
                "display_name": "Name",
                "type": "string",
                "required": True,
                "aliases": ["product", "product name", "title", "item"],
                "description": "Product name.",
            },
            {
                "name": "description",
                "display_name": "Description",
                "type": "string",
                "aliases": ["details", "desc"],
                "description": "Free-text description.",
            },
            {
                "name": "price",
                "display_name": "Price",
                "type": "decimal",
                "required": True,
                "aliases": ["unit price", "amount", "cost"],
                "description": "Unit price (decimal, no floats).",
                "rules": {"min": 0},
            },
            {
                "name": "currency",
                "display_name": "Currency",
                "type": "currency",
                "aliases": ["ccy", "curr"],
                "description": "ISO 4217 currency code.",
                "default": "USD",
            },
            {
                "name": "category",
                "display_name": "Category",
                "type": "string",
                "aliases": ["group", "type", "department"],
                "description": "Product category.",
            },
            {
                "name": "active",
                "display_name": "Active",
                "type": "boolean",
                "aliases": ["enabled", "is active", "available", "in stock"],
                "description": "Whether the product is sellable.",
                "default": "true",
            },
        ],
    },
    {
        "name": "Transaction Import",
        "description": "Payment transactions from a legacy processor.",
        "cross_field_rules": [
            {
                "then": {"field": "settled_at", "op": "gte_field", "value": "occurred_at"},
                "severity": "ERROR",
                "message": "settled_at must not be earlier than occurred_at",
            }
        ],
        "fields": [
            {
                "name": "transaction_id",
                "display_name": "Transaction ID",
                "type": "string",
                "required": True,
                "unique": True,
                "aliases": ["txn id", "trx id", "reference", "rrn", "id"],
                "description": "Unique transaction reference.",
            },
            {
                "name": "merchant_id",
                "display_name": "Merchant ID",
                "type": "string",
                "required": True,
                "aliases": ["mid", "merchant"],
                "description": "Merchant identifier.",
            },
            {
                "name": "terminal_id",
                "display_name": "Terminal ID",
                "type": "string",
                "aliases": ["tid", "terminal", "pos id"],
                "description": "POS terminal identifier.",
            },
            {
                "name": "amount",
                "display_name": "Amount",
                "type": "decimal",
                "required": True,
                "aliases": ["sum", "total", "value"],
                "description": "Transaction amount (decimal).",
            },
            {
                "name": "currency",
                "display_name": "Currency",
                "type": "currency",
                "required": True,
                "aliases": ["ccy", "curr"],
                "description": "ISO 4217 currency code.",
            },
            {
                "name": "status",
                "display_name": "Status",
                "type": "enum",
                "aliases": ["state", "result"],
                "description": "Transaction outcome.",
                "rules": {"enum": ["approved", "declined", "refunded", "voided"]},
            },
            {
                "name": "occurred_at",
                "display_name": "Occurred at",
                "type": "datetime",
                "required": True,
                "aliases": ["date", "timestamp", "transaction date", "time"],
                "description": "When the transaction happened.",
            },
            {
                "name": "settled_at",
                "display_name": "Settled at",
                "type": "datetime",
                "aliases": ["settlement date", "settled"],
                "description": "When it was settled.",
            },
        ],
    },
]


def seed_builtin_schemas(db: Session) -> None:
    existing = {s.name for s in db.scalars(select(ImportSchema)).all()}
    for spec in BUILTIN_SCHEMAS:
        if spec["name"] in existing:
            continue
        create_schema(db, spec, is_builtin=True, commit=False)
    db.commit()


def _normalize_field(raw: dict, position: int) -> SchemaField:
    name = (raw.get("name") or "").strip()
    if not name:
        raise InvalidInputError("Every field needs a name.")
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise InvalidInputError(
            f"Field name '{name}' must be a valid identifier (letters, digits, underscore).",
            details=[{"field": name}],
        )
    ftype = (raw.get("type") or "string").lower()
    if ftype not in FIELD_TYPES:
        raise InvalidInputError(f"Unknown field type '{ftype}'.", details=[{"field": name}])
    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",") if a.strip()]
    rules = dict(raw.get("rules") or {})
    if ftype == "enum" and not rules.get("enum"):
        raise InvalidInputError(
            f"Enum field '{name}' needs allowed values.", details=[{"field": name}]
        )
    return SchemaField(
        position=position,
        name=name,
        display_name=raw.get("display_name") or name.replace("_", " ").capitalize(),
        type=ftype,
        required=bool(raw.get("required", False)),
        unique=bool(raw.get("unique", False)),
        description=raw.get("description") or "",
        aliases=[str(a) for a in aliases],
        example=raw.get("example") or "",
        default=(raw.get("default") if raw.get("default") not in ("", None) else None),
        rules=rules,
        allow_multiple_sources=bool(raw.get("allow_multiple_sources", False)),
        source=_field_source(raw.get("source")),
    )


def _check_source(source) -> None:
    contract = source.get("contract") if isinstance(source, dict) else None
    if contract is None:
        return
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(contract)
    except SchemaError as exc:
        raise InvalidInputError(
            f"source.contract is not a valid JSON Schema: {exc.message}"
        ) from exc


def _field_source(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not (isinstance(path, list) and path and all(isinstance(p, str) and p for p in path)):
        return None
    return dict(raw)


def create_schema(
    db: Session, spec: dict, is_builtin: bool = False, commit: bool = True
) -> ImportSchema:
    name = (spec.get("name") or "").strip()
    if not name:
        raise InvalidInputError("Schema name is required.")
    fields_raw = spec.get("fields") or []
    if not fields_raw:
        raise InvalidInputError("A schema needs at least one field.")
    names = [f.get("name") for f in fields_raw]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise InvalidInputError(f"Duplicate field names: {', '.join(sorted(dupes))}.")
    _check_source(spec.get("source"))
    schema = ImportSchema(
        name=name,
        description=spec.get("description") or "",
        is_builtin=is_builtin,
        cross_field_rules=list(spec.get("cross_field_rules") or []),
        source=spec.get("source") if isinstance(spec.get("source"), dict) else None,
    )
    for i, raw in enumerate(fields_raw):
        schema.fields.append(_normalize_field(raw, i))
    db.add(schema)
    if commit:
        db.commit()
        db.refresh(schema)
    return schema


def update_schema(db: Session, schema_id: str, spec: dict) -> ImportSchema:
    schema = get_schema(db, schema_id)
    if spec.get("name"):
        schema.name = spec["name"].strip()
    if "description" in spec:
        schema.description = spec.get("description") or ""
    if isinstance(spec.get("source"), dict):
        _check_source(spec["source"])
        schema.source = spec["source"]
    if "cross_field_rules" in spec:
        schema.cross_field_rules = list(spec.get("cross_field_rules") or [])
    if "fields" in spec:
        fields_raw = spec["fields"] or []
        if not fields_raw:
            raise InvalidInputError("A schema needs at least one field.")
        names = [f.get("name") for f in fields_raw]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise InvalidInputError(f"Duplicate field names: {', '.join(sorted(dupes))}.")
        schema.fields.clear()
        db.flush()
        for i, raw in enumerate(fields_raw):
            schema.fields.append(_normalize_field(raw, i))
    schema.version += 1
    db.commit()
    db.refresh(schema)
    return schema


def get_schema(db: Session, schema_id: str) -> ImportSchema:
    schema = db.get(ImportSchema, schema_id)
    if schema is None:
        raise NotFoundError(f"Schema '{schema_id}' not found.")
    return schema


def list_schemas(db: Session) -> list[ImportSchema]:
    return list(
        db.scalars(
            select(ImportSchema).order_by(ImportSchema.is_builtin.desc(), ImportSchema.created_at)
        ).all()
    )


def delete_schema(db: Session, schema_id: str) -> None:
    schema = get_schema(db, schema_id)
    db.delete(schema)
    db.commit()


def source_summary(source: dict | None) -> dict | None:
    """Schema origin without the (possibly large) contract body."""
    if not source:
        return None
    out = {k: v for k, v in source.items() if k != "contract"}
    out["has_contract"] = bool(source.get("contract"))
    return out


def schema_to_dict(schema: ImportSchema) -> dict:
    return {
        "id": schema.id,
        "name": schema.name,
        "description": schema.description,
        "version": schema.version,
        "is_builtin": schema.is_builtin,
        "cross_field_rules": schema.cross_field_rules or [],
        "source": source_summary(schema.source),
        "created_at": schema.created_at.isoformat() if schema.created_at else None,
        "fields": [
            {
                "name": f.name,
                "display_name": f.display_name,
                "type": f.type,
                "required": f.required,
                "unique": f.unique,
                "description": f.description,
                "aliases": f.aliases or [],
                "example": f.example,
                "default": f.default,
                "rules": f.rules or {},
                "allow_multiple_sources": f.allow_multiple_sources,
                "source": f.source,
            }
            for f in schema.fields
        ],
    }
