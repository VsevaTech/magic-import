"""Normalized rows → nested API request payloads, and the contract check.

A schema built from an API contract remembers, per field, the JSON path and JSON type of
the property it came from. Here each normalized row is turned back into the request body
the API expects (``address_city`` → ``{"address": {"city": …}}``), with real JSON types:
integers, exact decimals (written from ``Decimal``, never through a float), booleans and
lists. Then every payload is validated against the stored contract (JSON Schema).
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from jsonschema import Draft202012Validator

_INT_RE = re.compile(r"^[+-]?\d+$")
_LIST_SPLIT = re.compile(r"\s*[;,|]\s*")
MAX_ERRORS = 200


def contract_fields(schema) -> list:
    return [f for f in schema.fields if (f.source or {}).get("path")]


def _coerce(value: str, json_type: str | None) -> Any:
    if json_type == "integer" and _INT_RE.match(value):
        return int(value)
    if json_type == "number":
        try:
            d = Decimal(value)
        except InvalidOperation:
            return value
        if d.is_finite():
            return d
        return value
    if json_type == "boolean":
        low = value.lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return value  # left as text; the contract check reports it


def row_payload(rec: dict, fields: list) -> dict:
    out: dict = {}
    for f in fields:
        src = f.source or {}
        path = src.get("path") or []
        value = rec.get(f.name)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if src.get("nullable") and src.get("required_in_contract"):
                _set(out, path, None)
            continue
        value = str(value)
        if src.get("list"):
            items = [x for x in _LIST_SPLIT.split(value.strip()) if x]
            _set(out, path, [_coerce(x, src.get("item_type")) for x in items])
        else:
            _set(out, path, _coerce(value, src.get("json_type")))
    return out


def _set(obj: dict, path: list[str], value: Any) -> None:
    node = obj
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[path[-1]] = value


def build_payloads(rows: list[dict], schema) -> list[dict]:
    fields = contract_fields(schema)
    return [row_payload(rec, fields) for rec in rows]


# ----------------------------------------------------------------------------- JSON
def dumps(obj: Any, indent: int = 2) -> str:
    """JSON text in which ``Decimal`` is written as an exact number (no float round-trip)."""
    parts: list[str] = []
    _write(obj, parts, indent, 0)
    return "".join(parts)


def _write(obj: Any, out: list[str], indent: int, level: int) -> None:
    pad = "\n" + " " * (indent * (level + 1))
    end = "\n" + " " * (indent * level)
    if isinstance(obj, dict):
        if not obj:
            out.append("{}")
            return
        out.append("{")
        for i, (k, v) in enumerate(obj.items()):
            out.append(("," if i else "") + pad + json.dumps(str(k), ensure_ascii=False) + ": ")
            _write(v, out, indent, level + 1)
        out.append(end + "}")
    elif isinstance(obj, list):
        if not obj:
            out.append("[]")
            return
        out.append("[")
        for i, v in enumerate(obj):
            out.append(("," if i else "") + pad)
            _write(v, out, indent, level + 1)
        out.append(end + "]")
    elif isinstance(obj, Decimal):
        out.append(format(obj, "f"))
    else:
        out.append(json.dumps(obj, ensure_ascii=False))


def to_payload_json(payloads: list[dict]) -> bytes:
    return dumps(payloads).encode("utf-8")


# ----------------------------------------------------------------------------- check
def check_payloads(payloads: list[dict], contract: dict, row_numbers: list[int]) -> dict:
    """Validate every payload against the contract. Row numbers are 1-based data rows."""
    validator = Draft202012Validator(contract)
    invalid = 0
    errors: list[dict] = []
    by_message: dict[str, int] = {}
    for payload, row in zip(payloads, row_numbers, strict=True):
        errs = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
        if not errs:
            continue
        invalid += 1
        for e in errs:
            path = "/".join(str(p) for p in e.absolute_path) or "(body)"
            msg = _short(e)
            key = f"{path}: {e.validator}"
            by_message[key] = by_message.get(key, 0) + 1
            if len(errors) < MAX_ERRORS:
                errors.append({"row": row, "path": path, "keyword": e.validator, "message": msg})
    top = sorted(by_message.items(), key=lambda kv: -kv[1])[:10]
    return {
        "checked": len(payloads),
        "valid": len(payloads) - invalid,
        "invalid": invalid,
        "errors": errors,
        "top_errors": [{"key": k, "count": c} for k, c in top],
    }


def _short(e) -> str:
    msg = e.message
    return msg if len(msg) <= 200 else msg[:197] + "…"
