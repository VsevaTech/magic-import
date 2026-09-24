"""Validation engine: normalized rows -> issues (ERROR / WARNING / INFO) + row status."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services import normalizers as norm

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"

# normalizer warning code -> (severity, human message)
TYPE_ISSUES: dict[str, tuple[str, str]] = {
    "email_invalid": (ERROR, "Invalid email"),
    "phone_invalid": (ERROR, "Invalid phone number"),
    "phone_unparseable": (WARNING, "Could not normalize phone"),
    "country_unknown": (WARNING, "Unknown country"),
    "currency_unknown": (WARNING, "Unknown currency"),
    "currency_ambiguous": (WARNING, "Ambiguous currency symbol"),
    "boolean_invalid": (ERROR, "Invalid boolean"),
    "decimal_invalid": (ERROR, "Invalid decimal"),
    "decimal_ambiguous": (WARNING, "Ambiguous decimal separator"),
    "integer_invalid": (ERROR, "Invalid integer"),
    "date_invalid": (ERROR, "Invalid date"),
    "date_ambiguous": (WARNING, "Ambiguous date (day/month order assumed)"),
    "datetime_invalid": (ERROR, "Invalid date-time"),
    "url_invalid": (ERROR, "Invalid URL"),
}

ISSUE_TITLES = {
    "required_missing": "Missing required value",
    "duplicate": "Duplicate value",
    "min_length": "Too short",
    "max_length": "Too long",
    "regex": "Does not match pattern",
    "enum": "Value not allowed",
    "min": "Below minimum",
    "max": "Above maximum",
    "min_date": "Date too early",
    "max_date": "Date too late",
    "cross_field": "Cross-field rule failed",
    "required_field_unmapped": "Required field missing",
    "leading_trailing_space": "Leading/trailing whitespace",
}
ISSUE_TITLES.update({code: msg for code, (_sev, msg) in TYPE_ISSUES.items()})


@dataclass
class Issue:
    row_index: int | None
    field: str | None
    severity: str
    code: str
    message: str
    original_value: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationResult:
    issues: list[Issue]
    row_status: list[str]

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(self.row_status)
        return {
            "ready": c.get("ready", 0),
            "warning": c.get("warning", 0),
            "error": c.get("error", 0),
        }


def _severity_rank(s: str) -> int:
    return {ERROR: 2, WARNING: 1, INFO: 0}[s]


def _type_check(ftype: str, value: str) -> str | None:
    """Return a normalizer warning code if ``value`` is not a valid ``ftype`` value."""
    if ftype == "string" or ftype == "enum":
        return None
    fn = norm.TYPE_NORMALIZERS.get(ftype)
    if fn is None:
        return None
    if ftype == "phone":
        r = fn(value, None)
    else:
        r = fn(value)
    if r.warning and r.warning.endswith("_ambiguous"):
        return None  # already reported by the transformation step
    if ftype == "country" and r.warning is None and value != r.value:
        return "country_unknown"  # a country field must hold ISO alpha-2 codes
    return r.warning


class ValidationEngine:
    def __init__(
        self,
        fields: list,
        mapping: dict[str, str | None],
        cross_field_rules: list[dict] | None = None,
        transform_notes: dict[tuple[int, str], str] | None = None,
        provided_fields: set[str] | None = None,
    ):
        self.fields = fields
        self.field_by_name = {f.name: f for f in fields}
        self.mapped_targets = {t for t in mapping.values() if t}
        if provided_fields:
            self.mapped_targets |= provided_fields
        self.rules = cross_field_rules or []
        self.notes = transform_notes or {}

    # ------------------------------------------------------------------ full run
    def run(self, rows: list[dict], original_rows: list[dict] | None = None) -> ValidationResult:
        issues: list[Issue] = []
        # dataset-level issues
        for f in self.fields:
            if f.required and f.name not in self.mapped_targets and f.default in (None, ""):
                issues.append(
                    Issue(
                        None,
                        f.name,
                        ERROR,
                        "required_field_unmapped",
                        f'Required field "{f.name}" is not mapped to any column',
                    )
                )
        dup_index = self._duplicate_index(rows)
        for i, rec in enumerate(rows):
            orig = original_rows[i] if original_rows else None
            issues.extend(self.validate_row(i, rec, dup_index, orig))
        for (i, fname), code in self.notes.items():
            sev, msg = TYPE_ISSUES.get(code, (WARNING, code))
            issues.append(
                Issue(i, fname, sev, code, msg, rows[i].get(fname) if i < len(rows) else None)
            )
        return ValidationResult(issues, self.row_statuses(issues, len(rows)))

    def _duplicate_index(self, rows: list[dict]) -> dict[str, set[str]]:
        """field -> set of values that occur more than once."""
        dup: dict[str, set[str]] = {}
        for f in self.fields:
            if not f.unique:
                continue
            counter = Counter(rec.get(f.name) for rec in rows if rec.get(f.name) not in (None, ""))
            dup[f.name] = {v for v, c in counter.items() if c > 1}
        return dup

    def duplicate_index(self, rows: list[dict]) -> dict[str, set[str]]:
        return self._duplicate_index(rows)

    # ------------------------------------------------------------------ one row
    def validate_row(
        self,
        i: int,
        rec: dict,
        dup_index: dict[str, set[str]],
        original: dict | None = None,
    ) -> list[Issue]:
        issues: list[Issue] = []
        for f in self.fields:
            value = rec.get(f.name)
            orig_value = original.get(f.name) if original else None
            if value in (None, ""):
                if f.required and (f.name in self.mapped_targets or f.default not in (None, "")):
                    issues.append(
                        Issue(
                            i,
                            f.name,
                            ERROR,
                            "required_missing",
                            "Missing required value",
                            orig_value,
                        )
                    )
                continue
            svalue = str(value)
            if f.name in dup_index and svalue in dup_index[f.name]:
                issues.append(Issue(i, f.name, ERROR, "duplicate", "Duplicate value", orig_value))
            code = _type_check(f.type, svalue)
            if code:
                sev, msg = TYPE_ISSUES[code]
                issues.append(
                    Issue(i, f.name, sev, code, msg, orig_value if orig_value else svalue)
                )
                if sev == ERROR:
                    continue
                # a doubtful value (warning) still has to satisfy the explicit rules: a
                # phone that could not be normalized must not slip past a pattern
            issues.extend(self._rule_checks(i, f, svalue, orig_value))
        issues.extend(self._cross_field(i, rec))
        return issues

    def _rule_checks(self, i: int, f, value: str, orig) -> list[Issue]:
        out: list[Issue] = []
        rules = f.rules or {}
        if "min_length" in rules and len(value) < int(rules["min_length"]):
            out.append(
                Issue(
                    i,
                    f.name,
                    ERROR,
                    "min_length",
                    f"Shorter than {rules['min_length']} characters",
                    orig,
                )
            )
        if "max_length" in rules and len(value) > int(rules["max_length"]):
            out.append(
                Issue(
                    i,
                    f.name,
                    ERROR,
                    "max_length",
                    f"Longer than {rules['max_length']} characters",
                    orig,
                )
            )
        if rules.get("regex"):
            try:
                if not re.fullmatch(rules["regex"], value):
                    out.append(
                        Issue(
                            i, f.name, ERROR, "regex", "Does not match the required pattern", orig
                        )
                    )
            except re.error:
                pass
        if rules.get("enum"):
            allowed = rules["enum"]
            if value not in allowed and value.lower() not in {str(a).lower() for a in allowed}:
                out.append(
                    Issue(
                        i,
                        f.name,
                        ERROR,
                        "enum",
                        f"Value not in allowed list ({', '.join(map(str, allowed[:6]))})",
                        orig,
                    )
                )
        if f.type in ("integer", "decimal"):
            try:
                num = Decimal(value)
            except InvalidOperation:
                num = None
            if num is not None:
                if "min" in rules and num < Decimal(str(rules["min"])):
                    out.append(
                        Issue(i, f.name, ERROR, "min", f"Below minimum {rules['min']}", orig)
                    )
                if "max" in rules and num > Decimal(str(rules["max"])):
                    out.append(
                        Issue(i, f.name, ERROR, "max", f"Above maximum {rules['max']}", orig)
                    )
        if f.type in ("date", "datetime"):
            d = _to_date(value)
            if d is not None:
                if rules.get("min_date") and d < date.fromisoformat(rules["min_date"]):
                    out.append(
                        Issue(
                            i, f.name, ERROR, "min_date", f"Earlier than {rules['min_date']}", orig
                        )
                    )
                if rules.get("max_date") and d > date.fromisoformat(rules["max_date"]):
                    out.append(
                        Issue(i, f.name, ERROR, "max_date", f"Later than {rules['max_date']}", orig)
                    )
        return out

    # ------------------------------------------------------------------ cross-field
    def _cross_field(self, i: int, rec: dict) -> list[Issue]:
        """Declarative rules::

        {"when": {"field": "country", "op": "eq", "value": "IL"},
         "then": {"field": "phone", "op": "phone_region", "value": "IL"},
         "severity": "WARNING", "message": "..."}
        {"then": {"field": "end_date", "op": "gte_field", "value": "start_date"}}
        """
        out: list[Issue] = []
        for rule in self.rules:
            cond = rule.get("when")
            if cond and not _evaluate(cond, rec):
                continue
            then = rule.get("then")
            if not then:
                continue
            target_value = rec.get(then.get("field"))
            if target_value in (None, ""):
                continue
            if not _evaluate(then, rec):
                sev = rule.get("severity", WARNING)
                msg = (
                    rule.get("message")
                    or f"Rule failed: {then.get('field')} {then.get('op')} {then.get('value')}"
                )
                out.append(Issue(i, then.get("field"), sev, "cross_field", msg, str(target_value)))
        return out

    # ------------------------------------------------------------------ statuses
    @staticmethod
    def row_statuses(issues: list[Issue], n_rows: int) -> list[str]:
        worst: dict[int, int] = defaultdict(int)
        for iss in issues:
            if iss.row_index is None:
                continue
            worst[iss.row_index] = max(worst[iss.row_index], _severity_rank(iss.severity))
        return [
            "error" if worst.get(i, 0) == 2 else "warning" if worst.get(i, 0) == 1 else "ready"
            for i in range(n_rows)
        ]


def _to_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _evaluate(clause: dict, rec: dict) -> bool:
    field = clause.get("field")
    op = clause.get("op", "eq")
    expected = clause.get("value")
    actual = rec.get(field)
    if op == "eq":
        return str(actual) == str(expected)
    if op == "neq":
        return str(actual) != str(expected)
    if op == "in":
        return actual in (expected or [])
    if op == "not_empty":
        return actual not in (None, "")
    if op == "empty":
        return actual in (None, "")
    if op == "phone_region":
        region = norm.phone_region(actual)
        return region is None or region == str(expected).upper()
    if op in ("gte_field", "lte_field", "gt_field", "lt_field"):
        other = rec.get(expected)
        if actual in (None, "") or other in (None, ""):
            return True
        a, b = _comparable(actual), _comparable(other)
        if a is None or b is None:
            return True
        return {
            "gte_field": a >= b,
            "lte_field": a <= b,
            "gt_field": a > b,
            "lt_field": a < b,
        }[op]
    if op in ("gte", "lte", "gt", "lt"):
        a, b = _comparable(actual), _comparable(expected)
        if a is None or b is None:
            return True
        return {"gte": a >= b, "lte": a <= b, "gt": a > b, "lt": a < b}[op]
    return True


def _comparable(value):
    s = str(value)
    d = _to_date(s)
    if d is not None and len(s) >= 10 and s[4] == "-":
        return d
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def top_issues(issues: list[Issue], limit: int = 8) -> list[dict]:
    """Group issues by (code, field) for the data-quality dashboard."""
    counter: Counter[tuple[str, str, str | None]] = Counter()
    for iss in issues:
        counter[(iss.code, iss.severity, iss.field)] += 1
    out = []
    for (code, sev, fname), count in counter.most_common(limit):
        title = ISSUE_TITLES.get(code, code)
        out.append(
            {
                "code": code,
                "severity": sev,
                "field": fname,
                "message": f"{title} · {fname}" if fname else title,
                "count": count,
            }
        )
    return out
