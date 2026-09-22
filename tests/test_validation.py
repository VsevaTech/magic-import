from types import SimpleNamespace

from app.services.validation_engine import ValidationEngine, top_issues


def F(name, type="string", required=False, unique=False, rules=None, default=None):  # noqa: N802
    return SimpleNamespace(
        name=name, type=type, required=required, unique=unique, rules=rules or {}, default=default
    )


FIELDS = [
    F("id", unique=True),
    F("name", required=True, rules={"min_length": 2, "max_length": 10}),
    F("email", "email", required=True, unique=True),
    F("phone", "phone"),
    F("country", "country"),
    F("status", "enum", rules={"enum": ["active", "blocked"]}),
    F("code", rules={"regex": r"^[A-Z]{3}-\d+$"}),
    F("age", "integer", rules={"min": 0, "max": 120}),
    F("joined", "date", rules={"min_date": "2000-01-01", "max_date": "2030-12-31"}),
    F("start", "date"),
    F("end", "date"),
]
MAPPING = {f.name: f.name for f in FIELDS}
RULES = [
    {
        "when": {"field": "country", "op": "eq", "value": "IL"},
        "then": {"field": "phone", "op": "phone_region", "value": "IL"},
        "severity": "WARNING",
        "message": "IL phone expected",
    },
    {
        "then": {"field": "end", "op": "gte_field", "value": "start"},
        "severity": "ERROR",
        "message": "end before start",
    },
]


def good(**over):
    rec = {
        "id": "1",
        "name": "Dana",
        "email": "dana@example.com",
        "phone": "+972525551234",
        "country": "IL",
        "status": "active",
        "code": "ABC-1",
        "age": "30",
        "joined": "2026-01-01",
        "start": "2026-01-01",
        "end": "2026-02-01",
    }
    rec.update(over)
    return rec


def validate(rows, rules=None, mapping=None):
    return ValidationEngine(FIELDS, mapping or MAPPING, rules or []).run(rows)


def codes(result, row=0):
    return sorted((i.field, i.code, i.severity) for i in result.issues if i.row_index == row)


def test_clean_row_is_ready():
    res = validate([good()])
    assert res.issues == [] and res.row_status == ["ready"]


def test_required_and_unique():
    res = validate(
        [good(name=None), good(id="1", email="dana@example.com"), good(id="1", email="x@y.com")]
    )
    assert ("name", "required_missing", "ERROR") in codes(res, 0)
    assert ("id", "duplicate", "ERROR") in codes(res, 1) and ("id", "duplicate", "ERROR") in codes(
        res, 2
    )
    assert ("email", "duplicate", "ERROR") in codes(res, 1)
    assert res.row_status == ["error", "error", "error"]


def test_type_checks():
    res = validate([good(email="bad", phone="05012ABC", country="Narnia", age="x", joined="nope")])
    c = codes(res)
    assert ("email", "email_invalid", "ERROR") in c
    assert ("phone", "phone_invalid", "ERROR") in c
    assert ("country", "country_unknown", "WARNING") in c
    assert ("age", "integer_invalid", "ERROR") in c
    assert ("joined", "date_invalid", "ERROR") in c


def test_country_must_be_iso_code_after_transformation():
    res = validate([good(country="Israel")])  # not normalized -> flagged
    assert ("country", "country_unknown", "WARNING") in codes(res)
    assert res.row_status == ["warning"]


def test_rules_enum_regex_length_range_dates():
    res = validate([good(status="deleted", code="abc-1", name="X", age="200", joined="1999-01-01")])
    c = {code for _f, code, _s in codes(res)}
    assert {"enum", "regex", "min_length", "max", "min_date"} <= c
    assert ("status", "enum", "ERROR") in codes(res)
    assert validate([good(status="Active")]).issues == []  # case-insensitive enum match


def test_cross_field_rules():
    res = validate(
        [good(phone="+12125550123"), good(id="2", email="b@x.com", end="2025-01-01")], rules=RULES
    )
    assert ("phone", "cross_field", "WARNING") in codes(res, 0)
    assert ("end", "cross_field", "ERROR") in codes(res, 1)
    assert res.row_status == ["warning", "error"]
    ok = validate([good(country="US", phone="+12125550123")], rules=RULES)
    assert ok.issues == []


def test_required_field_not_mapped_is_dataset_level_issue():
    mapping = {k: v for k, v in MAPPING.items() if k != "email"}
    res = validate([good(email=None)], mapping=mapping)
    ds = [i for i in res.issues if i.row_index is None]
    assert ds and ds[0].code == "required_field_unmapped" and ds[0].field == "email"
    # the row itself is not blamed for a missing column
    assert ("email", "required_missing", "ERROR") not in codes(res)


def test_transform_notes_become_warnings():
    engine = ValidationEngine(
        FIELDS, MAPPING, [], transform_notes={(0, "joined"): "date_ambiguous"}
    )
    res = engine.run([good()])
    assert codes(res) == [("joined", "date_ambiguous", "WARNING")]
    assert res.row_status == ["warning"]


def test_top_issues_grouping():
    res = validate(
        [good(email="bad"), good(email="worse", id="2"), good(name=None, id="3", email="c@d.com")]
    )
    top = top_issues(res.issues)
    assert top[0]["code"] == "email_invalid" and top[0]["count"] == 2 and top[0]["field"] == "email"
    assert top[1]["code"] == "required_missing" and top[1]["count"] == 1


def test_row_statuses_worst_severity_wins():
    from app.services.validation_engine import Issue

    issues = [
        Issue(0, "a", "WARNING", "x", "m"),
        Issue(0, "b", "ERROR", "y", "m"),
        Issue(2, "a", "INFO", "z", "m"),
    ]
    assert ValidationEngine.row_statuses(issues, 3) == ["error", "ready", "ready"]
