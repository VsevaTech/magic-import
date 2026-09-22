"""The demo files carry known, deliberately injected problems (see generate_demo_data.py)."""

from tests.conftest import DEMO
from tests.test_api import run_to_validated


def _codes(client, jid):
    items = client.get(f"/api/v1/imports/{jid}/issues?page_size=1000").json()["items"]
    out: dict[str, int] = {}
    for i in items:
        key = f"{i['code']}:{i['field']}"
        out[key] = out.get(key, 0) + 1
    return out


def test_acme_customers_expected_problems(client):
    job = run_to_validated(
        client, "acme-customers.xlsx", (DEMO / "acme-customers.xlsx").read_bytes()
    )
    c = _codes(client, job["id"])
    assert job["row_count"] == 500
    assert c["email_invalid:email"] == 9
    assert c["duplicate:customer_id"] == 12  # 6 injected duplicates = 12 rows sharing an id
    assert c["required_missing:customer_name"] == 5
    assert c["phone_invalid:phone"] == 12
    assert c["country_unknown:country"] == 7
    assert c["date_invalid:created_at"] == 3
    assert job["summary"]["error"] == 47 and job["summary"]["warning"] == 8


def test_legacy_crm_expected_problems(client):
    job = run_to_validated(client, "legacy-crm.csv", (DEMO / "legacy-crm.csv").read_bytes())
    c = _codes(client, job["id"])
    assert job["row_count"] == 300 and job["delimiter"] == ";" and job["encoding"] == "UTF-8-SIG"
    assert c["email_invalid:email"] == 6 and c["required_missing:customer_name"] == 4
    assert (
        c["phone_invalid:phone"] == 8
        and c["country_unknown:country"] == 4
        and c["date_invalid:created_at"] == 2
    )


def test_partner_export_expected_problems(client):
    job = run_to_validated(
        client, "partner-export.xlsx", (DEMO / "partner-export.xlsx").read_bytes()
    )
    c = _codes(client, job["id"])
    assert job["row_count"] == 400 and job["sheet_names"] == ["Customers", "Read me"]
    assert (
        c["email_invalid:email"] == 5
        and c["required_missing:customer_name"] == 3
        and c["phone_invalid:phone"] == 6
    )
    assert "country_unknown:country" not in c  # partner already sends ISO codes
