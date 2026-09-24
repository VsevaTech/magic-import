"""Import Schema from an API contract: parsing, $ref, composition, type mapping, API flow."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services import contract_import as ci
from app.services import payload_builder as pb
from app.services.errors import InvalidInputError
from tests.conftest import DEMO

MERCHANT_SPEC = (DEMO / "merchant-api.yaml").read_text(encoding="utf-8")


def oas(schema: dict, version: str = "3.1.0", media: str = "application/json") -> dict:
    return {
        "openapi": version,
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "requestBody": {"content": {media: {"schema": schema}}},
                    "responses": {"201": {"description": "ok"}},
                }
            }
        },
    }


def build(schema: dict, **kw) -> dict:
    return ci.build_schema(oas(schema, **kw), "POST /things")


def by_name(draft: dict) -> dict:
    return {f["name"]: f for f in draft["fields"]}


# ----------------------------------------------------------------------------- loading
def test_load_json_and_yaml_and_keeps_dates_as_text():
    assert ci.load_spec('{"openapi": "3.1.0"}')["openapi"] == "3.1.0"
    spec = ci.load_spec("openapi: 3.0.3\ninfo: {example: 2026-01-01}\n")
    assert spec["info"]["example"] == "2026-01-01"  # not a datetime.date


@pytest.mark.parametrize(
    "content,msg",
    [
        ("", "empty"),
        ("- a\n- b\n", "object"),
        ("{not json: [", "JSON or YAML"),
        ("!!python/object/apply:os.system ['echo hi']", "JSON or YAML"),
    ],
)
def test_load_rejects_bad_documents(content, msg):
    with pytest.raises(ci.InvalidSpecError, match=msg):
        ci.load_spec(content)


def test_load_size_limit():
    with pytest.raises(ci.InvalidSpecError, match="larger than"):
        ci.load_spec(b"{" + b" " * (ci.MAX_SPEC_BYTES + 1) + b"}")


def test_detect_format():
    assert ci.detect_format({"openapi": "3.1.0"}) == "openapi-3.1"
    assert ci.detect_format({"openapi": "3.0.3"}) == "openapi-3.0"
    assert ci.detect_format({"swagger": "2.0"}) == "swagger-2.0"
    assert ci.detect_format({"type": "object", "properties": {}}) == "json-schema"
    with pytest.raises(ci.InvalidSpecError):
        ci.detect_format({"hello": "world"})


# ----------------------------------------------------------------------------- targets
def test_demo_spec_targets():
    spec = ci.load_spec(MERCHANT_SPEC)
    targets = {t["id"]: t for t in ci.preview_targets(spec)}
    create = targets["POST /v1/merchants"]
    assert create["operation_id"] == "createMerchant"
    assert create["content_type"] == "application/json"
    assert create["field_count"] == 18
    assert targets["PATCH /v1/merchants/{merchantId}"]["content_type"].endswith("+json")
    assert "#/components/schemas/Address" in targets
    # an operation reached by operationId as well
    assert ci.get_target(spec, "createTerminal").id == "POST /v1/terminals"
    with pytest.raises(ci.InvalidSpecError, match="not found"):
        ci.get_target(spec, "GET /nope")


def test_swagger2_body_and_form_parameters():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "2"},
        "definitions": {
            "Pet": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            }
        },
        "paths": {
            "/pets": {
                "post": {
                    "parameters": [
                        {"in": "body", "name": "body", "schema": {"$ref": "#/definitions/Pet"}}
                    ]
                }
            },
            "/login": {
                "post": {
                    "parameters": [
                        {"in": "formData", "name": "user", "type": "string", "required": True},
                        {"in": "formData", "name": "remember", "type": "boolean"},
                    ]
                }
            },
        },
    }
    pets = by_name(ci.build_schema(spec, "POST /pets"))
    assert pets["name"]["required"] and pets["age"]["type"] == "integer"
    login = by_name(ci.build_schema(spec, "POST /login"))
    assert login["user"]["required"] and login["remember"]["type"] == "boolean"


def test_plain_json_schema_root_and_defs():
    spec = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Employee",
        "type": "object",
        "required": ["email"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "team": {"$ref": "#/$defs/Team"},
        },
        "$defs": {"Team": {"type": "object", "properties": {"code": {"type": "string"}}}},
    }
    ids = [t["id"] for t in ci.preview_targets(spec)]
    assert ids == ["#", "#/$defs/Team"]
    fields = by_name(ci.build_schema(spec, "#"))
    assert fields["email"]["type"] == "email" and fields["email"]["required"]
    assert fields["team_code"]["source"]["path"] == ["team", "code"]


# ----------------------------------------------------------------------------- mapping
def test_demo_spec_fields():
    draft = ci.build_schema(ci.load_spec(MERCHANT_SPEC), "POST /v1/merchants")
    f = by_name(draft)
    assert draft["name"] == "Acme Payments API — Create merchant"
    assert f["externalId"]["unique"] and f["externalId"]["required"]
    assert "merchant ref" in f["externalId"]["aliases"]  # x-aliases
    assert f["email"]["type"] == "email" and f["email"]["required"]
    assert f["legalName"]["rules"] == {"min_length": 2, "max_length": 140}
    assert f["mcc"]["rules"]["regex"] == r"^\d{4}$"
    assert f["monthlyVolume"]["type"] == "decimal" and f["monthlyVolume"]["rules"] == {"min": "0"}
    assert f["acceptsTips"]["type"] == "boolean" and f["acceptsTips"]["default"] == "false"
    assert f["riskLevel"]["type"] == "enum"
    assert f["riskLevel"]["rules"]["enum"] == ["low", "medium", "high"]
    assert f["onboardedAt"]["type"] == "date"
    assert f["website"]["type"] == "url" and f["website"]["source"]["nullable"]
    assert f["tags"]["source"]["list"] and f["tags"]["source"]["item_type"] == "string"
    # $ref'd scalar with pattern + name hint
    assert f["settlementCurrency"]["type"] == "currency"
    assert f["settlementCurrency"]["rules"]["regex"] == "^[A-Z]{3}$"
    assert f["address_country"]["type"] == "country" and f["address_country"]["required"]
    assert f["phone"]["type"] == "phone"
    # nested object flattened, path kept, leaf aliases added when unambiguous
    assert f["address_city"]["source"]["path"] == ["address", "city"]
    assert f["address_city"]["required"]
    assert "city" in f["address_city"]["aliases"]
    assert not f["address_line2"]["required"]
    skipped = {s["path"]: s["reason"] for s in draft["skipped"]}
    assert "readOnly" in skipped["id"]
    assert "array of objects" in skipped["owners"]
    assert "id" not in f and "owners" not in f


def test_type_and_format_mapping():
    f = by_name(
        build(
            {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "minimum": 1, "maximum": 9},
                    "amount": {"type": "number", "format": "double"},
                    "at": {"type": "string", "format": "date-time"},
                    "uid": {"type": "string", "format": "uuid"},
                    "home": {"type": "string", "format": "uri"},
                    "status": {"const": "new"},
                    "kind": {"type": "integer", "enum": [1, 2, 3]},
                    "untyped": {"description": "no type at all"},
                    "cc": {"type": "string", "description": "not ISO-restricted"},
                    "countryName": {"type": "string"},
                },
            }
        )
    )
    assert f["n"]["type"] == "integer" and f["n"]["rules"] == {"min": "1", "max": "9"}
    assert f["amount"]["type"] == "decimal"
    assert f["at"]["type"] == "datetime"
    assert f["uid"]["rules"]["regex"].startswith("^[0-9a-fA-F]{8}")
    assert f["home"]["type"] == "url"
    assert f["status"]["rules"]["enum"] == ["new"] and f["status"]["default"] is None
    assert f["kind"]["type"] == "enum" and f["kind"]["source"]["json_type"] == "integer"
    assert f["untyped"]["type"] == "string"
    # a free-text country name is NOT turned into an ISO country field
    assert f["countryName"]["type"] == "string"


def test_exclusive_bounds_30_and_31():
    f30 = by_name(
        build(
            {
                "type": "object",
                "properties": {
                    "i": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
                    "d": {"type": "number", "maximum": 10, "exclusiveMaximum": True},
                },
            },
            version="3.0.3",
        )
    )
    assert f30["i"]["rules"] == {"min": "1"}
    assert f30["d"]["rules"] == {"max": "10"}
    f31 = by_name(
        build({"type": "object", "properties": {"i": {"type": "integer", "exclusiveMaximum": 5}}})
    )
    assert f31["i"]["rules"] == {"max": "4"}


def test_patterns_become_fullmatch_regexes():
    f = by_name(
        build(
            {
                "type": "object",
                "properties": {
                    "anchored": {"type": "string", "pattern": "^[A-Z]{3}-\\d+$"},
                    "contains": {"type": "string", "pattern": "[0-9]"},
                    "ecma": {"type": "string", "pattern": "(?<year>\\d{4})"},
                },
            }
        )
    )
    import re

    assert re.fullmatch(f["anchored"]["rules"]["regex"], "ABC-12")
    rx = f["contains"]["rules"]["regex"]
    assert re.fullmatch(rx, "abc1def") and not re.fullmatch(rx, "abcdef")
    assert "regex" not in f["ecma"]["rules"]  # Python-incompatible: left to the contract check


def test_all_of_one_of_nullable_and_cycles():
    spec = oas({"$ref": "#/components/schemas/Order"}, version="3.0.3")
    spec["components"] = {
        "schemas": {
            "Base": {
                "type": "object",
                "required": ["ref"],
                "properties": {"ref": {"type": "string"}},
            },
            "Order": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {
                        "type": "object",
                        "properties": {
                            "note": {"type": "string", "nullable": True},
                            "payer": {
                                "oneOf": [
                                    {
                                        "type": "object",
                                        "required": ["kind", "iban"],
                                        "properties": {
                                            "kind": {"type": "string"},
                                            "iban": {"type": "string"},
                                        },
                                    },
                                    {
                                        "type": "object",
                                        "required": ["kind", "card"],
                                        "properties": {
                                            "kind": {"type": "string"},
                                            "card": {"type": "string"},
                                        },
                                    },
                                ]
                            },
                            "maybe": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                            "parent": {"$ref": "#/components/schemas/Order"},
                            "meta": {"type": "object", "additionalProperties": True},
                            "external": {"$ref": "other.yaml#/Foo"},
                        },
                    },
                ]
            },
        }
    }
    draft = ci.build_schema(spec, "POST /things")
    f = by_name(draft)
    assert f["ref"]["required"]
    assert f["note"]["source"]["nullable"] and not f["note"]["required"]
    assert f["maybe"]["type"] == "integer" and f["maybe"]["source"]["nullable"]
    assert {"payer_kind", "payer_iban", "payer_card"} <= set(f)
    assert not f["payer_iban"]["required"]
    assert any("oneOf" in n["message"] for n in draft["report"])
    skipped = {s["path"]: s["reason"] for s in draft["skipped"]}
    assert "recursive" in skipped["parent"]
    assert "free-form" in skipped["meta"]
    assert "external" in skipped["external"]
    # the contract is dereferenced, the recursive branch is cut, 3.0 nullable converted
    contract = draft["source"]["contract"]
    text = json.dumps(contract)
    assert "$ref" not in text
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(contract)
    assert Draft202012Validator(contract).is_valid({"ref": "A", "note": None})


def test_required_inside_optional_object_is_not_required_per_row():
    draft = build(
        {
            "type": "object",
            "properties": {
                "billing": {
                    "type": "object",
                    "required": ["city"],
                    "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
                }
            },
        }
    )
    f = by_name(draft)
    assert not f["billing_city"]["required"]
    assert f["billing_city"]["source"]["required_in_contract"]
    assert any("required inside optional" in n["message"] for n in draft["report"])


def test_name_collisions_and_identifier_cleanup():
    draft = build(
        {
            "type": "object",
            "properties": {
                "a_b": {"type": "string"},
                "a": {"type": "object", "properties": {"b": {"type": "string"}}},
                "2fa-code": {"type": "string"},
            },
        }
    )
    names = [f["name"] for f in draft["fields"]]
    assert names == ["a_b", "a_b_2", "f_2fa_code"]


def test_body_not_an_object_and_nothing_importable():
    with pytest.raises(ci.InvalidSpecError, match="not an object"):
        build({"type": "string"})
    with pytest.raises(ci.InvalidSpecError, match="no scalar properties"):
        build({"type": "object", "properties": {"file": {"type": "string", "format": "binary"}}})


def test_overrides():
    spec = ci.load_spec(MERCHANT_SPEC)
    draft = ci.build_schema(spec, "POST /v1/merchants")
    out = ci.apply_overrides(
        draft,
        {
            "tags": {"include": False},
            "email": {"include": False},
            "address.city": {"name": "city", "unique": True},
            "phone": {"type": "string"},
        },
    )
    f = by_name(out)
    assert "tags" not in f and "email" not in f
    assert f["city"]["unique"] and f["city"]["source"]["path"] == ["address", "city"]
    assert f["phone"]["type"] == "string"
    assert any(n["path"] == "email" and "excluded" in n["message"] for n in out["report"])
    with pytest.raises(InvalidInputError, match="Unknown property"):
        ci.apply_overrides(ci.build_schema(spec, "POST /v1/merchants"), {"nope": {}})
    with pytest.raises(InvalidInputError, match="cannot become an enum"):
        ci.apply_overrides(
            ci.build_schema(spec, "POST /v1/merchants"), {"legalName": {"type": "enum"}}
        )


# ----------------------------------------------------------------------------- payloads
class _F:
    def __init__(self, name, **source):
        self.name = name
        self.source = source


def test_row_payload_nesting_types_and_exact_decimals():
    fields = [
        _F("id", path=["id"], json_type="string"),
        _F("qty", path=["qty"], json_type="integer"),
        _F("price", path=["price"], json_type="number"),
        _F("ok", path=["ok"], json_type="boolean"),
        _F("city", path=["address", "city"], json_type="string"),
        _F("tags", path=["tags"], json_type="array", list=True, item_type="string"),
        _F("note", path=["note"], json_type="string", nullable=True, required_in_contract=True),
        _F("opt", path=["opt"], json_type="string"),
    ]
    rec = {
        "id": "A-1",
        "qty": "3",
        "price": "0.10",
        "ok": "true",
        "city": "Dubai",
        "tags": "a; b ,c",
        "note": None,
        "opt": "",
    }
    p = pb.row_payload(rec, fields)
    assert p == {
        "id": "A-1",
        "qty": 3,
        "price": Decimal("0.10"),
        "ok": True,
        "address": {"city": "Dubai"},
        "tags": ["a", "b", "c"],
        "note": None,
    }
    text = pb.dumps([p])
    assert '"price": 0.10' in text  # exact, no float round trip
    assert json.loads(text)[0]["address"] == {"city": "Dubai"}
    # an unconvertible value stays text so the contract check can report it
    assert pb.row_payload({"qty": "3.5"}, fields[1:2]) == {"qty": "3.5"}


def test_check_payloads_reports_rows_and_paths():
    contract = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}},
    }
    res = pb.check_payloads([{"id": "a", "qty": 2}, {"qty": 0}], contract, [2, 3])
    assert res["checked"] == 2 and res["valid"] == 1 and res["invalid"] == 1
    assert {e["keyword"] for e in res["errors"]} == {"required", "minimum"}
    assert all(e["row"] == 3 for e in res["errors"])


# ----------------------------------------------------------------------------- API flow
def _merchant_schema(client, **extra) -> dict:
    r = client.post(
        "/api/v1/schemas/from-contract",
        json={"content": MERCHANT_SPEC, "target": "POST /v1/merchants", **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_api_inspect_preview_create(client):
    r = client.post("/api/v1/schemas/from-contract/inspect", json={"content": MERCHANT_SPEC})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "openapi-3.1" and body["title"] == "Acme Payments API"
    assert body["targets"][0]["id"] == "POST /v1/merchants"

    r = client.post(
        "/api/v1/schemas/from-contract/preview",
        json={"content": MERCHANT_SPEC, "target": "createMerchant"},
    )
    assert r.status_code == 200 and len(r.json()["fields"]) == 18
    assert not any(s["name"].startswith("Acme") for s in client.get("/api/v1/schemas").json())

    schema = _merchant_schema(
        client, name="Merchants via API", overrides={"tags": {"include": False}}
    )
    assert schema["name"] == "Merchants via API" and len(schema["fields"]) == 17
    assert schema["source"]["target"] == "POST /v1/merchants"
    assert schema["source"]["has_contract"] and "contract" not in schema["source"]
    contract = client.get(f"/api/v1/schemas/{schema['id']}/contract").json()
    assert contract["$schema"].endswith("2020-12/schema")
    # the builder round-trip keeps the JSON paths
    fields = schema["fields"]
    r = client.put(f"/api/v1/schemas/{schema['id']}", json={"fields": fields})
    assert r.status_code == 200
    assert r.json()["fields"][-1]["source"]["path"] == ["address", "country"]
    assert r.json()["source"]["has_contract"]


def test_api_errors(client):
    r = client.post("/api/v1/schemas/from-contract/inspect", json={"content": "just text"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_spec"
    r = client.post(
        "/api/v1/schemas/from-contract/preview",
        json={"content": MERCHANT_SPEC, "target": "DELETE /x"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_spec"
    r = client.post(
        "/api/v1/schemas/from-contract",
        json={"content": MERCHANT_SPEC, "target": "POST /v1/merchants", "overrides": {"x": {}}},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    sid = client.get("/api/v1/schemas").json()[0]["id"]
    assert client.get(f"/api/v1/schemas/{sid}/contract").status_code == 404
    r = client.post(
        "/api/v1/schemas",
        json={
            "name": "bad",
            "fields": [{"name": "a"}],
            "source": {"contract": {"type": "no-such-type"}},
        },
    )
    assert r.status_code == 422


def test_full_flow_openapi_to_verified_payloads(client):
    schema = _merchant_schema(client)
    data = (DEMO / "merchant-onboarding.xlsx").read_bytes()
    job = client.post("/api/v1/imports", files={"file": ("merchant-onboarding.xlsx", data)}).json()
    r = client.post(f"/api/v1/imports/{job['id']}/schema", json={"schema_id": schema["id"]})
    mapping = {m["source_column"]: m for m in r.json()["mapping"]}
    assert all(m["confidence"] == "HIGH" for m in mapping.values()), mapping
    assert mapping["Merchant Ref"]["target_field"] == "externalId"
    assert mapping["City"]["target_field"] == "address_city"
    assert mapping["Postcode"]["target_field"] == "address_postalCode"
    client.post(
        f"/api/v1/imports/{job['id']}/mapping", json={"mapping": {}, "confirm": True}
    ).raise_for_status()
    summary = client.post(f"/api/v1/imports/{job['id']}/validate").json()["summary"]
    assert (summary["ready"], summary["warning"], summary["error"]) == (100, 4, 16)

    # every importable row becomes a payload the API contract accepts
    check = client.get(f"/api/v1/imports/{job['id']}/contract-check").json()
    assert check["target"] == "POST /v1/merchants"
    assert check["checked"] == 104 and check["invalid"] == 0
    assert check["fields_not_in_contract"] == []
    # error rows are exactly the ones the API would reject
    check_all = client.get(f"/api/v1/imports/{job['id']}/contract-check?scope=all").json()
    assert check_all["checked"] == 120 and check_all["invalid"] > 0
    assert {e["keyword"] for e in check_all["errors"]} >= {"pattern", "required", "minimum"}

    r = client.get(f"/api/v1/imports/{job['id']}/export?format=payload")
    assert r.status_code == 200 and "payloads.json" in r.headers["content-disposition"]
    payloads = json.loads(r.content, parse_float=Decimal)
    assert len(payloads) == 104
    first = payloads[0]
    assert set(first["address"]) >= {"line1", "city", "country"}
    assert isinstance(first["acceptsTips"], bool)
    assert isinstance(first["monthlyVolume"], Decimal)
    assert first["riskLevel"] in {"low", "medium", "high"}  # "Medium" → canonical spelling
    assert all(p["phone"].startswith("+") for p in payloads if "phone" in p)


def test_payload_export_needs_a_contract_schema(client):
    from tests.conftest import make_csv

    job = client.post(
        "/api/v1/imports",
        files={"file": ("c.csv", make_csv(["name", "email"], [["Dana", "dana@example.com"]]))},
    ).json()
    sid = next(
        s["id"] for s in client.get("/api/v1/schemas").json() if s["name"] == "Customer Import v1"
    )
    client.post(f"/api/v1/imports/{job['id']}/schema", json={"schema_id": sid})
    client.post(f"/api/v1/imports/{job['id']}/mapping", json={"mapping": {}, "confirm": True})
    client.post(f"/api/v1/imports/{job['id']}/validate")
    r = client.get(f"/api/v1/imports/{job['id']}/export?format=payload")
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"
    assert client.get(f"/api/v1/imports/{job['id']}/contract-check").status_code == 409


def test_enum_values_take_the_allowed_spelling():
    from app.services.transformation_engine import TransformationEngine

    class F:
        def __init__(self, name, type_, rules=None):
            self.name, self.type, self.rules, self.default = name, type_, rules or {}, None

    engine = TransformationEngine(
        ["Status"], {"Status": "status"}, [F("status", "enum", {"enum": ["active", "Paused"]})], []
    )
    rows = engine.run([["ACTIVE"], ["paused"], ["other"], [None]]).rows
    assert [r["status"] for r in rows] == ["active", "Paused", "other", None]


def test_existing_database_gets_new_columns(tmp_path, monkeypatch):
    import sqlite3

    from app import config, db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schemas (id VARCHAR(32) PRIMARY KEY, name VARCHAR(120))")
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    config.get_settings.cache_clear()
    db.reset_engine_for_tests()
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(schemas)")}
    assert "source" in cols


def test_ui_contract_page_and_sample(client):
    r = client.get("/schemas/from-contract")
    assert r.status_code == 200 and "Import schema from OpenAPI" in r.text
    r = client.get("/samples/merchant-api.yaml")
    assert r.status_code == 200 and "openapi: 3.1.0" in r.text
    assert "Import from OpenAPI" in client.get("/schemas").text


def test_exponential_ref_expansion_is_bounded():
    import time

    schemas = {
        f"L{i}": {
            "type": "object",
            "properties": {f"p{j}": {"$ref": f"#/components/schemas/L{i + 1}"} for j in range(10)},
        }
        for i in range(8)
    }
    schemas["L8"] = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    spec = oas({"$ref": "#/components/schemas/L0"})
    spec["components"] = {"schemas": schemas}
    t0 = time.perf_counter()
    with pytest.raises(ci.InvalidSpecError, match="too complex"):
        ci.build_schema(spec, "POST /things")
    ci.preview_targets(spec)  # listing survives, oversized targets are just not offered
    assert time.perf_counter() - t0 < 5
