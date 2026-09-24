"""Build an Import Schema from an API contract (OpenAPI 3.x, Swagger 2.0 or JSON Schema).

    spec (JSON / YAML)  →  targets (POST /v1/merchants, …)  →  draft Import Schema

A request body is a tree; a spreadsheet row is flat. The builder walks the (dereferenced)
body schema and turns every scalar leaf into one schema field, remembering the JSON path
(``["address", "city"]``) so the export can rebuild the nested payload later. Everything it
cannot represent honestly — arrays of objects, free-form maps, binary uploads, external
``$ref`` — is skipped and listed in the build report instead of being guessed.

The builder also keeps a dereferenced JSON Schema (draft 2020-12) of the request body: the
*contract*. Exported payloads are checked against it, so the loop "OpenAPI → spreadsheet →
payloads the API accepts" is verified, not assumed.

Pure functions, no FastAPI / database dependency.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.services.errors import InvalidInputError

MAX_SPEC_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 6
MAX_FIELDS = 250
MAX_NAME = 80
# $ref / YAML aliases can make a small document expand exponentially; every walk has a budget
MAX_NODES = 20_000
BODY_METHODS = ("post", "put", "patch")
ALL_METHODS = ("post", "put", "patch", "delete", "get", "options", "head")

_JSON_MEDIA = re.compile(r"^application/(?:[\w.+-]+\+)?json$|^\*/\*$", re.I)
_FORM_MEDIA = ("application/x-www-form-urlencoded", "multipart/form-data")


class InvalidSpecError(InvalidInputError):
    code = "invalid_spec"


# ============================================================================ loading
def load_spec(content: str | bytes) -> dict:
    """Parse a JSON or YAML document into a dict (YAML with the safe loader only)."""
    if isinstance(content, bytes):
        if len(content) > MAX_SPEC_BYTES:
            raise InvalidSpecError(
                f"The specification is larger than {MAX_SPEC_BYTES // (1024 * 1024)} MB."
            )
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InvalidSpecError("The specification must be UTF-8 text.") from exc
    if len(content.encode("utf-8")) > MAX_SPEC_BYTES:
        raise InvalidSpecError(
            f"The specification is larger than {MAX_SPEC_BYTES // (1024 * 1024)} MB."
        )
    text = content.lstrip("﻿").strip()
    if not text:
        raise InvalidSpecError("The specification is empty.")
    data: Any
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = yaml.load(text, Loader=_SafeStringLoader)  # noqa: S506 - safe loader subclass
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark is not None else ""
            raise InvalidSpecError(f"Not valid JSON or YAML{where}.") from exc
    if not isinstance(data, dict):
        raise InvalidSpecError("The specification must be a JSON / YAML object.")
    return data


class _SafeStringLoader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as strings (``example: 2026-01-01`` stays text)."""


_SafeStringLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def detect_format(spec: dict) -> str:
    oas = str(spec.get("openapi") or "")
    if oas.startswith("3.1") or oas.startswith("3.2"):
        return "openapi-3.1"
    if oas.startswith("3."):
        return "openapi-3.0"
    if str(spec.get("swagger") or "").startswith("2"):
        return "swagger-2.0"
    if any(k in spec for k in ("properties", "$schema", "$defs", "definitions", "allOf")) or (
        spec.get("type") == "object"
    ):
        return "json-schema"
    raise InvalidSpecError(
        "Unrecognised document: expected an OpenAPI 3.x / Swagger 2.0 description "
        "(`openapi:` / `swagger:` key) or a JSON Schema with `properties`."
    )


# ============================================================================ $ref
class _UnresolvableError(Exception):
    pass


def _pointer(spec: dict, ref: str) -> Any:
    if not ref.startswith("#"):
        raise _UnresolvableError(f"external reference '{ref}' is not supported")
    node: Any = spec
    for raw in ref[1:].split("/")[1:] if ref != "#" else []:
        part = raw.replace("~1", "/").replace("~0", "~")
        part = re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), part)
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise _UnresolvableError(f"reference '{ref}' does not exist")
    return node


def _deref(spec: dict, node: Any, stack: tuple[str, ...]) -> tuple[Any, tuple[str, ...]]:
    """Follow ``$ref`` chains. Sibling keywords override the target (OAS 3.1 semantics)."""
    seen = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in stack:
            raise _CycleError(ref)
        target = _pointer(spec, ref)
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        stack = (*stack, ref)
        node = {**target, **siblings} if isinstance(target, dict) and siblings else target
        seen += 1
        if seen > 64:
            raise _UnresolvableError("reference chain too long")
    return node, stack


class _CycleError(Exception):
    def __init__(self, ref: str):
        super().__init__(ref)
        self.ref = ref


# ============================================================================ targets
@dataclass
class Target:
    id: str
    kind: str  # operation | schema
    label: str
    method: str | None = None
    path: str | None = None
    operation_id: str | None = None
    summary: str = ""
    content_type: str | None = None
    deprecated: bool = False
    body: Any = None  # the (possibly $ref) schema node of the body

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "method": self.method.upper() if self.method else None,
            "path": self.path,
            "operation_id": self.operation_id,
            "summary": self.summary,
            "content_type": self.content_type,
            "deprecated": self.deprecated,
        }


def _pick_media(content: dict) -> tuple[str | None, Any]:
    if not isinstance(content, dict):
        return None, None
    for media, obj in content.items():
        if _JSON_MEDIA.match(media.split(";")[0].strip()) and isinstance(obj, dict):
            return media, obj.get("schema")
    for media, obj in content.items():
        if media.split(";")[0].strip().lower() in _FORM_MEDIA and isinstance(obj, dict):
            return media, obj.get("schema")
    return None, None


def list_targets(spec: dict) -> list[Target]:
    fmt = detect_format(spec)
    out: list[Target] = []
    if fmt.startswith("openapi") or fmt == "swagger-2.0":
        paths = spec.get("paths") or {}
        for path, item in paths.items() if isinstance(paths, dict) else []:
            try:
                item, _ = _deref(spec, item, ())
            except (_UnresolvableError, _CycleError):
                continue
            if not isinstance(item, dict):
                continue
            for method in ALL_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                media, body = _operation_body(spec, fmt, item, op)
                if body is None:
                    continue
                label = f"{method.upper()} {path}"
                out.append(
                    Target(
                        id=label,
                        kind="operation",
                        label=label,
                        method=method,
                        path=path,
                        operation_id=op.get("operationId"),
                        summary=str(op.get("summary") or op.get("description") or "")[:200],
                        content_type=media,
                        deprecated=bool(op.get("deprecated")),
                        body=body,
                    )
                )
        comps = (
            (spec.get("components") or {}).get("schemas")
            if fmt.startswith("openapi")
            else spec.get("definitions")
        )
        prefix = "#/components/schemas/" if fmt.startswith("openapi") else "#/definitions/"
    else:
        out.append(
            Target(
                id="#",
                kind="schema",
                label=str(spec.get("title") or "Root schema"),
                summary=str(spec.get("description") or "")[:200],
                body=spec,
            )
        )
        comps = spec.get("$defs") or spec.get("definitions")
        prefix = "#/$defs/" if spec.get("$defs") else "#/definitions/"
    for name, node in (comps or {}).items() if isinstance(comps, dict) else []:
        try:
            resolved, _ = _deref(spec, node, ())
        except (_UnresolvableError, _CycleError):
            continue
        if not isinstance(resolved, dict) or not _looks_like_object(spec, resolved):
            continue
        ref = prefix + name.replace("~", "~0").replace("/", "~1")
        out.append(
            Target(
                id=ref,
                kind="schema",
                label=name,
                summary=str(resolved.get("description") or resolved.get("title") or "")[:200],
                body={"$ref": ref},
            )
        )
    return out


def _looks_like_object(spec: dict, node: dict) -> bool:
    if node.get("type") == "object" or "properties" in node:
        return True
    for key in ("allOf", "oneOf", "anyOf"):
        for part in node.get(key) or []:
            try:
                part, _ = _deref(spec, part, ())
            except (_UnresolvableError, _CycleError):
                continue
            if isinstance(part, dict) and (part.get("type") == "object" or "properties" in part):
                return True
    return False


def _operation_body(spec: dict, fmt: str, item: dict, op: dict) -> tuple[str | None, Any]:
    if fmt == "swagger-2.0":
        params = list(item.get("parameters") or []) + list(op.get("parameters") or [])
        form_props: dict = {}
        form_required: list[str] = []
        for p in params:
            try:
                p, _ = _deref(spec, p, ())
            except (_UnresolvableError, _CycleError):
                continue
            if not isinstance(p, dict):
                continue
            if p.get("in") == "body" and p.get("schema") is not None:
                consumes = op.get("consumes") or spec.get("consumes") or ["application/json"]
                return consumes[0], p["schema"]
            if p.get("in") == "formData" and p.get("name"):
                form_props[p["name"]] = {
                    k: v for k, v in p.items() if k not in ("in", "name", "required")
                }
                if p.get("required"):
                    form_required.append(p["name"])
        if form_props:
            return "application/x-www-form-urlencoded", {
                "type": "object",
                "properties": form_props,
                "required": form_required,
            }
        return None, None
    body = op.get("requestBody")
    if body is None:
        return None, None
    try:
        body, _ = _deref(spec, body, ())
    except (_UnresolvableError, _CycleError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    return _pick_media(body.get("content") or {})


def get_target(spec: dict, target_id: str) -> Target:
    for t in list_targets(spec):
        if t.id == target_id or (t.operation_id and t.operation_id == target_id):
            return t
    raise InvalidSpecError(
        f"Target '{target_id}' not found in the specification.",
        details=[{"target": target_id}],
    )


# ============================================================================ composition
@dataclass
class Note:
    level: str  # info | warning
    path: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "path": self.path, "message": self.message}


def _types(node: dict) -> tuple[list[str], bool]:
    """Declared JSON types without "null", plus whether null is allowed."""
    t = node.get("type")
    types = [t] if isinstance(t, str) else list(t) if isinstance(t, list) else []
    nullable = "null" in types or bool(node.get("nullable")) or bool(node.get("x-nullable"))
    types = [x for x in types if x != "null"]
    if not types:
        if "properties" in node or "additionalProperties" in node:
            types = ["object"]
        elif "items" in node:
            types = ["array"]
    return types, nullable


def _is_null_schema(node: Any) -> bool:
    return isinstance(node, dict) and (
        node.get("type") == "null" or (node.get("enum") == [None]) or node.get("const", 0) is None
    )


class _Composer:
    """Resolves $ref / allOf / oneOf / anyOf into one flat schema dict per node."""

    def __init__(self, spec: dict, notes: list[Note]):
        self.spec = spec
        self.notes = notes

    def compose(self, node: Any, stack: tuple[str, ...], path: str) -> tuple[dict, tuple]:
        node, stack = _deref(self.spec, node, stack)
        if not isinstance(node, dict):
            return {}, stack
        if not any(k in node for k in ("allOf", "oneOf", "anyOf")):
            return node, stack
        base = {k: v for k, v in node.items() if k not in ("allOf", "oneOf", "anyOf")}
        merged: dict = {}
        for part in node.get("allOf") or []:
            sub, _ = self.compose(part, stack, path)
            merged = _merge(merged, sub)
        for key in ("oneOf", "anyOf"):
            variants = node.get(key)
            if not variants:
                continue
            real = [v for v in variants if not _is_null_schema(_deref(self.spec, v, stack)[0])]
            if len(real) < len(variants):
                merged["nullable"] = True
            resolved = [self.compose(v, stack, path)[0] for v in real]
            if len(resolved) == 1:
                merged = _merge(merged, resolved[0])
                continue
            if resolved and all(_types(r)[0] in (["object"], []) for r in resolved):
                union: dict = {"type": "object", "properties": {}}
                required_sets = []
                for r in resolved:
                    for pname, pschema in (r.get("properties") or {}).items():
                        union["properties"].setdefault(pname, pschema)
                    required_sets.append(set(r.get("required") or []))
                union["required"] = sorted(set.intersection(*required_sets))
                merged = _merge(merged, union)
                self.notes.append(
                    Note(
                        "warning",
                        path or "(body)",
                        f"{key} with {len(resolved)} object variants: properties merged, only "
                        "properties required by every variant stay required.",
                    )
                )
            else:
                merged = _merge(merged, {"type": "string"})
                self.notes.append(
                    Note(
                        "warning",
                        path or "(body)",
                        f"{key} of different scalar types: imported as text.",
                    )
                )
        return _merge(merged, base), stack


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k == "properties" and isinstance(v, dict):
            out["properties"] = {**(out.get("properties") or {}), **v}
        elif k == "required" and isinstance(v, list):
            out["required"] = sorted({*(out.get("required") or []), *v})
        elif k == "nullable":
            out["nullable"] = bool(out.get("nullable")) or bool(v)
        else:
            out[k] = v
    return out


# ============================================================================ fields
_PHONE_HINT = re.compile(r"(phone|msisdn|mobile|tel(ephone)?|cell)(number|no)?$", re.I)
_COUNTRY_HINT = re.compile(r"country", re.I)
_CURRENCY_HINT = re.compile(r"(currency|ccy)", re.I)
_ALPHA2 = {r"^[A-Z]{2}$", r"^[a-zA-Z]{2}$", r"[A-Z]{2}", r"^[A-Z][A-Z]$"}
_ALPHA3 = {r"^[A-Z]{3}$", r"^[a-zA-Z]{3}$", r"[A-Z]{3}", r"^[A-Z][A-Z][A-Z]$"}
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _humanize(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    s = re.sub(r"[_\-.]+", " ", s).strip()
    return s[:1].upper() + s[1:].lower() if s else name


def _identifier(parts: list[str]) -> str:
    raw = "_".join(re.sub(r"[^A-Za-z0-9_]+", "_", p).strip("_") or "field" for p in parts)
    if not re.match(r"[A-Za-z_]", raw):
        raw = "f_" + raw
    return raw[:MAX_NAME]


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _python_regex(pattern: str) -> str | None:
    """JSON Schema patterns are unanchored ("contains"); validation uses fullmatch."""
    try:
        re.compile(pattern)
    except re.error:
        return None
    if pattern.startswith("^") and pattern.endswith("$") and not pattern.endswith(r"\$"):
        return pattern
    return f"(?s).*(?:{pattern}).*"


@dataclass
class _Leaf:
    path: list[str]
    node: dict
    json_type: str
    required: bool  # required all the way up
    required_here: bool  # required in its parent
    nullable: bool
    is_list: bool = False
    item_node: dict = field(default_factory=dict)


class ContractSchemaBuilder:
    def __init__(self, spec: dict):
        self.spec = spec
        self.format = detect_format(spec)
        self.notes: list[Note] = []
        self.skipped: list[dict] = []
        self.composer = _Composer(spec, self.notes)
        self.leaves: list[_Leaf] = []
        self.visits = 0

    # --------------------------------------------------------------- walk
    def _skip(self, path: list[str], reason: str) -> None:
        self.skipped.append({"path": _dotted(path) or "(body)", "reason": reason})

    def walk(self, node: Any, path: list[str], required: bool, required_here: bool, stack=()):
        self.visits += 1
        if self.visits > MAX_NODES:
            raise InvalidSpecError(
                f"The request body expands to more than {MAX_NODES:,} schema nodes "
                "(deeply repeated $ref?) — too complex to import."
            )
        dotted = _dotted(path)
        try:
            s, stack = self.composer.compose(node, stack, dotted)
        except _CycleError as c:
            self._skip(path, f"recursive reference {c.ref} — cut to avoid an infinite schema")
            return
        except _UnresolvableError as exc:
            self._skip(path, str(exc))
            return
        if s.get("readOnly"):
            self._skip(path, "readOnly — generated by the server, not part of a request")
            return
        types, nullable = _types(s)
        jtype = types[0] if len(types) == 1 else ("string" if types else None)
        if len(types) > 1:
            self.notes.append(Note("warning", dotted, f"several types {types}: imported as text"))
        if jtype is None:
            jtype = "string" if ("enum" in s or "const" in s or "format" in s or path) else None
        if jtype == "object":
            if len(path) >= MAX_DEPTH:
                self._skip(path, f"nested deeper than {MAX_DEPTH} levels")
                return
            props = s.get("properties") or {}
            if not props:
                self._skip(
                    path,
                    "free-form object (map / additionalProperties) — no fixed columns",
                )
                return
            req = set(s.get("required") or [])
            for pname, pnode in props.items():
                child_req = pname in req
                if child_req and not required and path:
                    self.notes.append(
                        Note(
                            "info",
                            _dotted([*path, pname]),
                            f"required inside optional '{dotted}': not enforced per row, "
                            "the contract check catches a partially filled object.",
                        )
                    )
                self.walk(pnode, [*path, pname], required and child_req, child_req, stack)
            return
        if not path:
            raise InvalidSpecError("The request body is not an object — nothing to map columns to.")
        if jtype == "array":
            try:
                item, _ = self.composer.compose(s.get("items") or {}, stack, dotted + "[]")
            except (_CycleError, _UnresolvableError):
                item = {}
            itypes, _ = _types(item)
            if (itypes and itypes[0] in ("object", "array")) or item.get("properties"):
                self._skip(
                    path,
                    "array of objects — one spreadsheet row is one request; import the list "
                    "items as a separate file",
                )
                return
            self.leaves.append(
                _Leaf(
                    path,
                    s,
                    itypes[0] if itypes else "string",
                    required,
                    required_here,
                    nullable,
                    True,
                    item,
                )
            )
            return
        if jtype == "string" and s.get("format") in ("binary", "byte", "base64"):
            self._skip(path, f"file content (format: {s.get('format')}) — cannot come from a cell")
            return
        if jtype not in ("string", "integer", "number", "boolean"):
            self._skip(path, f"unsupported type '{jtype}'")
            return
        self.leaves.append(_Leaf(path, s, jtype, required, required_here, nullable))

    # --------------------------------------------------------------- leaf → field
    def _field(self, leaf: _Leaf, name: str, leaf_alias_ok: bool) -> dict:
        s = leaf.item_node if leaf.is_list else leaf.node
        meta = leaf.node
        dotted = _dotted(leaf.path)
        leaf_name = leaf.path[-1]
        fmt = str(s.get("format") or "").lower()
        rules: dict = {}
        ftype = "string"
        enum = s.get("enum")
        if "const" in s:
            enum = [s["const"]]
        pattern = s.get("pattern")
        min_len, max_len = s.get("minLength"), s.get("maxLength")
        inferred = None

        if leaf.is_list:
            ftype = "string"
        elif enum:
            ftype = "enum"
            rules["enum"] = [_scalar(v) for v in enum if v is not None]
        elif leaf.json_type == "integer":
            ftype = "integer"
        elif leaf.json_type == "number":
            ftype = "decimal"
        elif leaf.json_type == "boolean":
            ftype = "boolean"
        elif fmt in ("email", "idn-email"):
            ftype = "email"
        elif fmt in ("uri", "url", "iri"):
            ftype = "url"
        elif fmt == "date":
            ftype = "date"
        elif fmt == "date-time":
            ftype = "datetime"
        elif fmt in ("phone", "tel", "e164", "phone-number"):
            ftype = "phone"
        elif fmt in ("iso-3166-1-alpha-2", "country-code", "country"):
            ftype = "country"
        elif fmt in ("iso-4217", "currency", "currency-code"):
            ftype = "currency"
        elif _COUNTRY_HINT.search(leaf_name) and (
            pattern in _ALPHA2 or (max_len == 2 and min_len in (None, 2))
        ):
            ftype, inferred = "country", "country field restricted to 2 letters"
        elif _CURRENCY_HINT.search(leaf_name) and (
            pattern in _ALPHA3 or (max_len == 3 and min_len in (None, 3))
        ):
            ftype, inferred = "currency", "currency field restricted to 3 letters"
        elif _PHONE_HINT.search(leaf_name) and not fmt:
            ftype, inferred = "phone", "name looks like a phone number"
        if inferred:
            self.notes.append(
                Note("info", dotted, f"type '{ftype}' inferred ({inferred}); change it if wrong")
            )
        if fmt == "uuid" and ftype == "string":
            rules["regex"] = f"^{_UUID_RE}$"

        # ---- rules
        if ftype in ("string", "email", "phone", "url") and not leaf.is_list:
            if isinstance(min_len, int):
                rules["min_length"] = min_len
            if isinstance(max_len, int):
                rules["max_length"] = max_len
        # the pattern is checked on the *normalized* value (E.164, ISO codes, …), exactly
        # what the payload will carry
        if ftype not in ("enum", "boolean", "integer", "decimal") and not leaf.is_list:
            if isinstance(pattern, str):
                rx = _python_regex(pattern)
                if rx is None:
                    self.notes.append(
                        Note(
                            "warning",
                            dotted,
                            "pattern uses syntax Python does not support; checked only by the "
                            "contract check",
                        )
                    )
                else:
                    rules["regex"] = rx
        if ftype in ("integer", "decimal"):
            lo, hi = s.get("minimum"), s.get("maximum")
            xlo, xhi = s.get("exclusiveMinimum"), s.get("exclusiveMaximum")
            if isinstance(xlo, bool):  # OAS 3.0 / draft-4 form
                xlo = lo if xlo else None
                lo = None if xlo is not None else lo
            if isinstance(xhi, bool):
                xhi = hi if xhi else None
                hi = None if xhi is not None else hi
            for bound, key, exclusive, step in (
                (lo, "min", xlo, 1),
                (hi, "max", xhi, -1),
            ):
                if exclusive is not None and not isinstance(exclusive, bool):
                    if ftype == "integer" and float(exclusive).is_integer():
                        rules[key] = str(int(exclusive) + step)
                    else:
                        rules[key] = _scalar(exclusive)
                        self.notes.append(
                            Note(
                                "warning",
                                dotted,
                                f"exclusive {key} {exclusive} validated as inclusive; the "
                                "contract check enforces the exact bound",
                            )
                        )
                elif bound is not None:
                    rules[key] = _scalar(bound)
            if s.get("multipleOf") is not None:
                self.notes.append(
                    Note("info", dotted, "multipleOf is enforced only by the contract check")
                )

        aliases: list[str] = []
        for key in ("x-aliases", "x-magic-import-aliases"):
            val = meta.get(key)
            if isinstance(val, list):
                aliases += [str(a) for a in val]
            elif isinstance(val, str):
                aliases += [a.strip() for a in val.split(",") if a.strip()]
        if len(leaf.path) > 1:
            aliases.append(" ".join(_humanize(p).lower() for p in leaf.path))
            if leaf_alias_ok:
                aliases.append(_humanize(leaf_name).lower())
        if meta.get("title"):
            aliases.append(str(meta["title"]))
        seen: set[str] = set()
        aliases = [a for a in aliases if not (a.lower() in seen or seen.add(a.lower()))]

        example = meta.get("example")
        if example is None and isinstance(meta.get("examples"), list) and meta["examples"]:
            example = meta["examples"][0]
        if example is None:
            example = meta.get("x-example")
        default = meta.get("default")
        if leaf.is_list:
            if isinstance(default, list):
                default = "; ".join(_scalar(v) for v in default)
            self.notes.append(
                Note(
                    "info",
                    dotted,
                    "list of values: separate items in the cell with ';' or ',' — split on export",
                )
            )
        description = str(meta.get("description") or "").strip()
        if leaf.is_list:
            description = (description + " (list: separate values with ';')").strip()
        display = str(meta.get("title") or "").strip() or " · ".join(
            _humanize(p) for p in leaf.path
        )
        if meta.get("deprecated"):
            self.notes.append(Note("info", dotted, "deprecated in the contract"))
        unique = bool(meta.get("x-unique") or meta.get("x-magic-import-unique"))
        return {
            "name": name,
            "display_name": display[:120],
            "type": ftype,
            "required": leaf.required and not leaf.nullable,
            "unique": unique,
            "description": description,
            "aliases": aliases,
            "example": "" if example is None else _scalar(example)[:200],
            "default": None if default is None else _scalar(default)[:200],
            "rules": rules,
            "allow_multiple_sources": False,
            "source": {
                "path": list(leaf.path),
                "json_type": leaf.json_type,
                "item_type": _types(s)[0][0] if leaf.is_list and _types(s)[0] else None,
                "list": leaf.is_list,
                "nullable": leaf.nullable,
                "required_in_contract": leaf.required_here,
                "format": fmt or None,
            },
        }

    # --------------------------------------------------------------- build
    def build(self, target: Target) -> dict:
        self.walk(target.body, [], True, True)
        if not self.leaves:
            raise InvalidSpecError(
                f"'{target.label}' has no scalar properties that could come from a spreadsheet.",
                details=self.skipped,
            )
        if len(self.leaves) > MAX_FIELDS:
            self.notes.append(
                Note("warning", "(body)", f"only the first {MAX_FIELDS} leaf properties are used")
            )
            self.leaves = self.leaves[:MAX_FIELDS]
        leaf_counts: dict[str, int] = {}
        for leaf in self.leaves:
            leaf_counts[leaf.path[-1].lower()] = leaf_counts.get(leaf.path[-1].lower(), 0) + 1
        fields: list[dict] = []
        used: set[str] = set()
        for leaf in self.leaves:
            name = _identifier(leaf.path)
            base, n = name, 2
            while name.lower() in used:
                name = f"{base[: MAX_NAME - 4]}_{n}"
                n += 1
            if name != base:
                self.notes.append(
                    Note(
                        "warning",
                        _dotted(leaf.path),
                        f"name '{base}' already taken, field renamed to '{name}'",
                    )
                )
            used.add(name.lower())
            fields.append(self._field(leaf, name, leaf_counts[leaf.path[-1].lower()] == 1))
        info = self.spec.get("info") or {}
        title = str(info.get("title") or self.spec.get("title") or "").strip()
        if target.kind == "operation":
            name = target.operation_id or target.label
            name = f"{_humanize(name) if target.operation_id else name}"
            if title:
                name = f"{title} — {name}"
        else:
            name = f"{title} — {target.label}" if title and title != target.label else target.label
        description = target.summary or f"Built from {target.label}"
        contract = build_contract(self.spec, target.body)
        return {
            "name": name[:120],
            "description": description[:500],
            "fields": fields,
            "cross_field_rules": [],
            "source": {
                "kind": "contract",
                "format": self.format,
                "title": title or None,
                "version": str(info.get("version")) if info.get("version") is not None else None,
                "target": target.id,
                "target_kind": target.kind,
                "method": target.method.upper() if target.method else None,
                "path": target.path,
                "operation_id": target.operation_id,
                "content_type": target.content_type,
                "contract": contract,
            },
            "report": [n.as_dict() for n in self.notes],
            "skipped": self.skipped,
        }


def _dotted(path: list[str]) -> str:
    return ".".join(path)


def build_schema(spec: dict, target_id: str) -> dict:
    target = get_target(spec, target_id)
    return ContractSchemaBuilder(spec).build(target)


def preview_targets(spec: dict) -> list[dict]:
    """Targets with the number of importable fields (cheap enough for real specs)."""
    out = []
    for t in list_targets(spec):
        d = t.as_dict()
        try:
            b = ContractSchemaBuilder(spec)
            b.walk(t.body, [], True, True)
            d["field_count"] = len(b.leaves)
            d["skipped_count"] = len(b.skipped)
        except Exception:  # a broken or oversized target must not hide the others
            d["field_count"] = 0
            d["skipped_count"] = 0
        if d["field_count"]:
            out.append(d)
    return out


# ============================================================================ contract
_DROP_KEYS = {"discriminator", "xml", "externalDocs", "example", "nullable", "x-nullable"}


def build_contract(spec: dict, body: Any) -> dict:
    """Dereferenced JSON Schema (draft 2020-12) of the body; recursion is cut to ``{}``."""
    oas30 = detect_format(spec) in ("openapi-3.0", "swagger-2.0")
    budget = [MAX_NODES]

    def conv(node: Any, stack: tuple[str, ...], depth: int) -> Any:
        budget[0] -= 1
        if budget[0] < 0:
            raise InvalidSpecError(
                f"The request body expands to more than {MAX_NODES:,} schema nodes — "
                "too complex to store as a contract."
            )
        if depth > 40:
            return {}
        if isinstance(node, list):
            return [conv(x, stack, depth + 1) for x in node]
        if not isinstance(node, dict):
            return node
        try:
            node, stack = _deref(spec, node, stack)
        except (_CycleError, _UnresolvableError):
            return {}
        if not isinstance(node, dict):
            return node
        out: dict = {}
        for k, v in node.items():
            if k in _DROP_KEYS or k.startswith("x-"):
                continue
            if k in ("properties", "patternProperties", "$defs", "definitions"):
                props = {
                    pk: conv(pv, stack, depth + 1)
                    for pk, pv in (v or {}).items()
                    if not (k == "properties" and _is_read_only(spec, pv, stack))
                }
                out[k] = props
            elif k in ("items", "additionalProperties", "not", "contains", "propertyNames"):
                out[k] = conv(v, stack, depth + 1)
            elif k in ("allOf", "oneOf", "anyOf", "prefixItems"):
                out[k] = [conv(x, stack, depth + 1) for x in v or []]
            else:
                out[k] = copy.deepcopy(v)
        if "required" in out and isinstance(node.get("properties"), dict):
            ro = {p for p, pv in node["properties"].items() if _is_read_only(spec, pv, stack)}
            out["required"] = [r for r in out["required"] if r not in ro]
            if not out["required"]:
                del out["required"]
        if oas30:
            for key, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
                if isinstance(out.get(key), bool):
                    if out[key] and bound in out:
                        out[key] = out.pop(bound)
                    else:
                        del out[key]
        if node.get("nullable") or node.get("x-nullable"):
            t = out.get("type")
            if isinstance(t, str):
                out["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                out["type"] = [*t, "null"]
            if isinstance(out.get("enum"), list) and None not in out["enum"]:
                out["enum"] = [*out["enum"], None]
        if out.get("format") in ("binary", "byte", "base64"):
            out.pop("format", None)
        return out

    result = conv(body, (), 0)
    if isinstance(result, dict):
        result.pop("$schema", None)
        result.pop("$id", None)
        result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return result


def _is_read_only(spec: dict, node: Any, stack: tuple) -> bool:
    try:
        n, _ = _deref(spec, node, stack)
    except (_CycleError, _UnresolvableError):
        return False
    return isinstance(n, dict) and bool(n.get("readOnly"))


# ============================================================================ overrides
_OVERRIDABLE = ("name", "display_name", "type", "required", "unique", "aliases", "default")


def apply_overrides(draft: dict, overrides: dict[str, dict]) -> dict:
    """Apply per-path user edits from the preview (exclude / rename / retype / flags)."""
    by_path = {_dotted(f["source"]["path"]): f for f in draft["fields"]}
    unknown = sorted(set(overrides) - set(by_path))
    if unknown:
        raise InvalidInputError(
            f"Unknown property path(s): {', '.join(unknown)}.",
            details=[{"path": p} for p in unknown],
        )
    fields = []
    for path, f in by_path.items():
        ov = overrides.get(path) or {}
        if ov.get("include", True) is False:
            if f["source"].get("required_in_contract") and f["required"]:
                draft["report"].append(
                    Note(
                        "warning",
                        path,
                        "excluded although the contract requires it — payloads will fail the "
                        "contract check unless the API has a default",
                    ).as_dict()
                )
            continue
        f = dict(f)
        for key in _OVERRIDABLE:
            if ov.get(key) is not None:
                f[key] = ov[key]
        if f["type"] == "enum" and not f["rules"].get("enum"):
            raise InvalidInputError(
                f"Field '{f['name']}' cannot become an enum: the contract lists no values.",
                details=[{"path": path}],
            )
        fields.append(f)
    if not fields:
        raise InvalidInputError("Every property was excluded — nothing left to import.")
    draft["fields"] = fields
    return draft
