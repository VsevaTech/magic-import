"""Transformation engine: original rows + mapping + transformations -> normalized rows.

Original values are never mutated; every run starts from the original rows so a
transformation can be disabled ("undo") and the data recomputed deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import normalizers as norm

# Transformation kinds that operate on one target field
FIELD_KINDS = {
    "trim",
    "null_normalize",
    "case",
    "date",
    "datetime",
    "boolean",
    "decimal",
    "integer",
    "phone",
    "country",
    "currency",
    "email",
    "url",
    "replace",
    "default",
}
# Kinds that create a target field from several sources / split one into several
STRUCTURAL_KINDS = {"combine", "split"}

KIND_LABELS = {
    "trim": "Trim whitespace",
    "null_normalize": "Normalize empty values (N/A, null, -)",
    "case": "Change case",
    "date": "Date normalization (ISO 8601)",
    "datetime": "Date-time normalization",
    "boolean": "Boolean normalization",
    "decimal": "Decimal normalization",
    "integer": "Integer normalization",
    "phone": "Phone normalization (E.164)",
    "country": "Country normalization (ISO 3166-1 alpha-2)",
    "currency": "Currency normalization (ISO 4217)",
    "email": "Email normalization",
    "url": "URL normalization",
    "replace": "Replace values",
    "default": "Default value",
    "combine": "Combine columns",
    "split": "Split column",
}

_TYPE_TO_KIND = {
    "date": "date",
    "datetime": "datetime",
    "boolean": "boolean",
    "decimal": "decimal",
    "integer": "integer",
    "phone": "phone",
    "country": "country",
    "currency": "currency",
    "email": "email",
    "url": "url",
}


@dataclass
class TransformSpec:
    kind: str
    target_field: str | None = None
    params: dict = field(default_factory=dict)
    enabled: bool = True
    label: str = ""
    id: str | None = None

    @classmethod
    def from_model(cls, t) -> TransformSpec:
        return cls(
            kind=t.kind,
            target_field=t.target_field,
            params=dict(t.params or {}),
            enabled=bool(t.enabled),
            label=t.label or "",
            id=t.id,
        )


@dataclass
class TransformResult:
    rows: list[dict[str, str | None]]
    # (row_index, field) -> warning code raised while normalizing (ambiguity etc.)
    notes: dict[tuple[int, str], str]


def suggest_transformations(
    fields: list, mapping: dict[str, str | None], profile: dict | None = None
) -> list[TransformSpec]:
    """Default transformation set for a mapped schema (all enabled, marked suggested)."""
    mapped_targets = {t for t in mapping.values() if t}
    specs: list[TransformSpec] = []
    field_by_name = {f.name: f for f in fields}
    # global hygiene first
    specs.append(TransformSpec("trim", None, {}, True, "Trim whitespace in all columns"))
    specs.append(
        TransformSpec(
            "null_normalize", None, {}, True, "Treat N/A, null, -, — as empty in all columns"
        )
    )
    # country / currency first: phone normalization uses the country as context
    ordered = sorted(
        (f.name for f in fields if f.name in mapped_targets),
        key=lambda n: 0 if field_by_name[n].type in ("country", "currency") else 1,
    )
    for name in ordered:
        f = field_by_name[name]
        kind = _TYPE_TO_KIND.get(f.type)
        if kind:
            params: dict = {}
            if kind == "phone":
                country_field = next((x.name for x in fields if x.type == "country"), None)
                if country_field and country_field in mapped_targets:
                    params["country_field"] = country_field
            specs.append(TransformSpec(kind, name, params, True, f"{KIND_LABELS[kind]}: {name}"))
    for f in fields:
        if f.default not in (None, "") and f.name not in mapped_targets:
            specs.append(
                TransformSpec(
                    "default",
                    f.name,
                    {"value": f.default},
                    True,
                    f"Default {f.name} = {f.default}",
                )
            )
    return specs


class TransformationEngine:
    def __init__(
        self,
        columns: list[str],
        mapping: dict[str, str | None],
        fields: list,
        transformations: list[TransformSpec],
        defaults: dict[str, str] | None = None,
    ):
        self.columns = columns
        self.col_index = {c: i for i, c in enumerate(columns)}
        self.mapping = {s: t for s, t in mapping.items() if t}
        self.fields = fields
        self.field_names = [f.name for f in fields]
        self.field_by_name = {f.name: f for f in fields}
        self.transformations = [t for t in transformations if t.enabled]
        self.defaults = dict(defaults or {})
        # target -> ordered source columns
        self.sources_for: dict[str, list[str]] = {}
        for src, tgt in self.mapping.items():
            self.sources_for.setdefault(tgt, []).append(src)

    # ------------------------------------------------------------------ public
    def infer_date_order(self, rows: list[list[str | None]]) -> None:
        """Column-level day/month order detection for date transformations.

        If any slashed value in the column can only be day-first (first part > 12)
        and none can only be month-first, the whole column is treated as day-first
        without per-cell ambiguity warnings (and vice versa).
        """
        for t in self.transformations:
            if t.kind not in ("date", "datetime") or "dayfirst" in t.params:
                continue
            srcs = self.sources_for.get(t.target_field or "", [])
            if not srcs:
                continue
            idx = self.col_index.get(srcs[0])
            if idx is None:
                continue
            day_first_only = month_first_only = 0
            for row in rows:
                v = row[idx] if idx < len(row) else None
                if not v:
                    continue
                m = re.match(r"^\s*(\d{1,2})[./-](\d{1,2})[./-]\d{2,4}", str(v))
                if not m:
                    continue
                a, b = int(m.group(1)), int(m.group(2))
                if a > 12 >= b:
                    day_first_only += 1
                elif b > 12 >= a:
                    month_first_only += 1
            if day_first_only and not month_first_only:
                t.params = {**t.params, "dayfirst": True, "inferred": True}
            elif month_first_only and not day_first_only:
                t.params = {**t.params, "dayfirst": False, "inferred": True}

    def run(
        self,
        rows: list[list[str | None]],
        overrides: dict[int, dict[str, str | None]] | None = None,
    ) -> TransformResult:
        """Transform all rows. ``overrides`` = manual cell edits per row index; they are
        applied before the field-level transformations so context-dependent rules (e.g.
        phone region from the country column) see the corrected value."""
        out: list[dict[str, str | None]] = []
        notes: dict[tuple[int, str], str] = {}
        overrides = overrides or {}
        for i, row in enumerate(rows):
            record, row_notes = self.transform_row(row, overrides.get(i))
            out.append(record)
            for f, code in row_notes.items():
                notes[(i, f)] = code
        return TransformResult(out, notes)

    def transform_row(
        self, row: list[str | None], overrides: dict[str, str | None] | None = None
    ) -> tuple[dict[str, str | None], dict[str, str]]:
        record: dict[str, str | None] = dict.fromkeys(self.field_names)
        notes: dict[str, str] = {}

        def cell(source: str) -> str | None:
            idx = self.col_index.get(source)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        for tgt, srcs in self.sources_for.items():
            if tgt not in record:
                continue
            value = None
            for s in srcs:
                value = cell(s)
                if value not in (None, ""):
                    break
            record[tgt] = value

        # structural transformations first
        for t in self.transformations:
            if t.kind == "combine" and t.target_field in record:
                record[t.target_field] = _combine(t.params.get("template", ""), cell, record)
            elif t.kind == "split":
                _split(t, cell, record)

        # defaults from the job / schema
        for name, value in self.defaults.items():
            if name in record and record[name] in (None, ""):
                record[name] = value

        # manual edits replace the mapped value; the rules below still run on them
        for name, value in (overrides or {}).items():
            if name in record:
                record[name] = value

        # field-level transformations, in order
        for t in self.transformations:
            if t.kind not in FIELD_KINDS:
                continue
            targets = [t.target_field] if t.target_field else self.field_names
            for name in targets:
                if name not in record:
                    continue
                value, warn = self._apply(t, name, record[name], record)
                record[name] = value
                if warn:
                    notes[name] = warn
        return record, notes

    # ------------------------------------------------------------------ internals
    def _apply(
        self, t: TransformSpec, name: str, value: str | None, record: dict
    ) -> tuple[str | None, str | None]:
        p = t.params
        kind = t.kind
        if kind == "default":
            if value in (None, ""):
                return p.get("value"), None
            return value, None
        if value is None:
            return None, None
        if kind == "trim":
            return norm.trim(value), None
        if kind == "null_normalize":
            return norm.normalize_null(value, set(p.get("tokens", []))), None
        if kind == "case":
            return norm.apply_case(value, p.get("mode", "title")), None
        if kind == "replace":
            table = p.get("mapping", {})
            return table.get(value, value), None
        if kind == "email":
            r = norm.normalize_email(value)
        elif kind == "phone":
            region = p.get("region")
            cf = p.get("country_field")
            if cf and record.get(cf):
                region = record.get(cf)
            r = norm.normalize_phone(value, region)
        elif kind == "country":
            r = norm.normalize_country(value)
        elif kind == "currency":
            r = norm.normalize_currency(value)
        elif kind == "boolean":
            r = norm.normalize_boolean(value)
        elif kind == "decimal":
            r = norm.normalize_decimal(value)
        elif kind == "integer":
            r = norm.normalize_integer(value)
        elif kind == "date":
            r = norm.normalize_date(value, p.get("dayfirst"))
        elif kind == "datetime":
            r = norm.normalize_datetime(value, p.get("dayfirst"))
        elif kind == "url":
            r = norm.normalize_url(value)
        else:
            return value, None
        # Only *ambiguity* is reported here; hard failures are found by validation,
        # which re-checks the type on the final value (avoids duplicate issues).
        warn = r.warning if r.warning and r.warning.endswith("_ambiguous") else None
        return r.value, warn


_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")


def _combine(template: str, cell, record: dict) -> str | None:
    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        v = cell(key)
        if v is None:
            v = record.get(key)
        return "" if v is None else str(v)

    out = _TEMPLATE_RE.sub(repl, template).strip()
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"^[,\s]+|[,\s]+$", "", out)
    return out or None


def _split(t: TransformSpec, cell, record: dict) -> None:
    p = t.params
    source = p.get("source")
    value = record.get(source) if source in record else cell(source)
    into: list[str] = p.get("into", [])
    if not into:
        return
    sep = p.get("separator", " ")
    mode = p.get("mode", "first_rest")  # first_rest | last_first | all
    if value is None:
        for f in into:
            if f in record and record[f] is None:
                record[f] = None
        return
    parts = [x for x in str(value).split(sep) if x != ""]
    if mode == "first_rest":
        pieces = [parts[0] if parts else None, sep.join(parts[1:]) or None]
    elif mode == "rest_last":
        pieces = [sep.join(parts[:-1]) or None, parts[-1] if parts else None]
    else:
        pieces = parts
    for f, piece in zip(into, pieces, strict=False):
        if f in record:
            record[f] = piece
