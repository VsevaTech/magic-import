from types import SimpleNamespace

from app.services.transformation_engine import (
    TransformationEngine,
    TransformSpec,
    suggest_transformations,
)


def F(name, type="string", default=None):  # noqa: N802 - tiny factory
    return SimpleNamespace(
        name=name, type=type, default=default, required=False, unique=False, rules={}
    )


FIELDS = [
    F("name"),
    F("email", "email"),
    F("phone", "phone"),
    F("country", "country"),
    F("joined", "date"),
    F("amount", "decimal"),
    F("active", "boolean"),
    F("address"),
    F("first_name"),
    F("last_name"),
    F("status", default="active"),
]
COLUMNS = [
    "Client",
    "Mail",
    "Mobile",
    "Country",
    "Joined",
    "Amount",
    "Active",
    "Street",
    "House",
    "City",
]
MAPPING = {
    "Client": "name",
    "Mail": "email",
    "Mobile": "phone",
    "Country": "country",
    "Joined": "joined",
    "Amount": "amount",
    "Active": "active",
}
ROW = [
    "  Dana  Levi ",
    "Dana@Example.COM",
    "052-555-1234",
    "Israel",
    "21/09/2026",
    "₪1,299.50",
    "Yes",
    "Herzl",
    "12",
    "Tel Aviv",
]


def run(specs, row=ROW, defaults=None):
    engine = TransformationEngine(COLUMNS, MAPPING, FIELDS, specs, defaults)
    return engine.run([row])


def test_suggested_transformations_follow_field_types_and_order():
    specs = suggest_transformations(FIELDS, MAPPING)
    kinds = [(s.kind, s.target_field) for s in specs]
    assert kinds[0] == ("trim", None) and kinds[1] == ("null_normalize", None)
    assert (
        ("phone", "phone") in kinds
        and ("country", "country") in kinds
        and ("date", "joined") in kinds
    )
    assert kinds.index(("country", "country")) < kinds.index(
        ("phone", "phone")
    )  # country context first
    assert ("default", "status") in kinds  # schema default for an unmapped field
    phone = next(s for s in specs if s.kind == "phone")
    assert phone.params == {"country_field": "country"}


def test_trim_null_case():
    res = run([TransformSpec("trim"), TransformSpec("case", "name", {"mode": "upper"})])
    assert res.rows[0]["name"] == "DANA LEVI"
    res = run([TransformSpec("null_normalize")], row=["N/A", *ROW[1:]])
    assert res.rows[0]["name"] is None


def test_typed_normalizers_with_country_context():
    specs = [
        TransformSpec("trim"),
        TransformSpec("country", "country"),
        TransformSpec("phone", "phone", {"country_field": "country"}),
        TransformSpec("email", "email"),
        TransformSpec("date", "joined"),
        TransformSpec("decimal", "amount"),
        TransformSpec("boolean", "active"),
    ]
    rec = run(specs).rows[0]
    assert rec["country"] == "IL"
    assert rec["phone"] == "+972525551234"
    assert rec["email"] == "Dana@example.com"
    assert rec["joined"] == "2026-09-21"
    assert rec["amount"] == "1299.50"
    assert rec["active"] == "true"


def test_ambiguity_is_reported_as_note():
    res = run([TransformSpec("date", "joined")], row=[*ROW[:4], "03/04/2026", *ROW[5:]])
    assert res.rows[0]["joined"] == "2026-04-03"
    assert res.notes[(0, "joined")] == "date_ambiguous"
    res = run([TransformSpec("decimal", "amount")], row=[*ROW[:5], "1,299", *ROW[6:]])
    assert res.notes[(0, "amount")] == "decimal_ambiguous"


def test_column_level_dayfirst_inference_removes_ambiguity():
    engine = TransformationEngine(COLUMNS, MAPPING, FIELDS, [TransformSpec("date", "joined")])
    rows = [[*ROW[:4], "03/04/2026", *ROW[5:]], [*ROW[:4], "25/12/2025", *ROW[5:]]]
    engine.infer_date_order(rows)
    res = engine.run(rows)
    assert res.rows[0]["joined"] == "2026-04-03" and res.rows[1]["joined"] == "2025-12-25"
    assert res.notes == {}


def test_disabled_transformation_is_skipped():
    res = run([TransformSpec("trim", enabled=False)])
    assert res.rows[0]["name"] == "  Dana  Levi "


def test_defaults_job_and_schema():
    res = run([TransformSpec("default", "status", {"value": "active"})])
    assert res.rows[0]["status"] == "active"
    res = run([], defaults={"status": "imported"})
    assert res.rows[0]["status"] == "imported"


def test_replace():
    res = run([TransformSpec("replace", "country", {"mapping": {"Israel": "IL"}})])
    assert res.rows[0]["country"] == "IL"


def test_combine_columns():
    res = run([TransformSpec("combine", "address", {"template": "{Street} {House}, {City}"})])
    assert res.rows[0]["address"] == "Herzl 12, Tel Aviv"
    res = run(
        [TransformSpec("combine", "address", {"template": "{Street} {House}, {City}"})],
        row=[*ROW[:7], None, None, "Haifa"],
    )
    assert res.rows[0]["address"] == "Haifa"


def test_split_column():
    res = run(
        [
            TransformSpec("trim"),
            TransformSpec("split", None, {"source": "name", "into": ["first_name", "last_name"]}),
        ]
    )
    assert res.rows[0]["first_name"] == "Dana" and res.rows[0]["last_name"] == "Levi"
    res = run(
        [
            TransformSpec(
                "split",
                None,
                {"source": "Client", "into": ["first_name", "last_name"], "mode": "rest_last"},
            )
        ],
        row=["Dana Bat Levi", *ROW[1:]],
    )
    assert res.rows[0]["first_name"] == "Dana Bat" and res.rows[0]["last_name"] == "Levi"


def test_original_rows_are_never_mutated():
    row = list(ROW)
    run([TransformSpec("trim"), TransformSpec("country", "country")], row=row)
    assert row == ROW


def test_multiple_sources_first_non_empty_wins():
    fields = [F("email", "email")]
    engine = TransformationEngine(["Mail", "Alt"], {"Mail": "email", "Alt": "email"}, fields, [])
    assert engine.run([[None, "alt@x.com"], ["main@x.com", "alt@x.com"]]).rows == [
        {"email": "alt@x.com"},
        {"email": "main@x.com"},
    ]
