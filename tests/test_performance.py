"""Benchmark-like check: 10,000 rows x 50 columns must run through the whole pipeline
(parse -> inspect -> map -> transform -> validate -> export) in reasonable time."""

import random
import time

import pytest

from tests.conftest import make_csv

ROWS, COLS = 10_000, 50
TIME_BUDGET_S = 45.0  # generous for slow CI runners; locally this is a few seconds


@pytest.mark.slow
def test_10k_rows_50_columns_end_to_end(client):
    rng = random.Random(1)
    headers = [
        "Client",
        "E-mail Address",
        "Mobile No.",
        "Organization",
        "Country Name",
        "Joined",
        "Customer Ref",
    ]
    headers += [f"Extra {i}" for i in range(COLS - len(headers))]
    countries = ["Israel", "IL", "United Kingdom", "US", "Deutschland"]
    rows = []
    for i in range(ROWS):
        rows.append(
            [
                f"Customer {i}",
                f"user{i}@example.com",
                f"+9725{rng.randint(0, 4)}{rng.randint(1000000, 9999999)}",
                f"Company {i % 100}",
                rng.choice(countries),
                f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}/2025",
                f"C-{i}",
                *[f"v{i}-{c}" for c in range(COLS - 7)],
            ]
        )
    data = make_csv(headers, rows)

    t0 = time.perf_counter()
    job = client.post("/api/v1/imports", files={"file": ("big.csv", data)}).json()
    t_upload = time.perf_counter() - t0
    assert job["row_count"] == ROWS and job["column_count"] == COLS

    sid = next(
        s["id"] for s in client.get("/api/v1/schemas").json() if s["name"] == "Customer Import v1"
    )
    t1 = time.perf_counter()
    client.post(f"/api/v1/imports/{job['id']}/schema", json={"schema_id": sid}).raise_for_status()
    client.post(
        f"/api/v1/imports/{job['id']}/mapping", json={"mapping": {}, "confirm": True}
    ).raise_for_status()
    t_map = time.perf_counter() - t1

    t2 = time.perf_counter()
    res = client.post(f"/api/v1/imports/{job['id']}/validate").json()
    t_validate = time.perf_counter() - t2
    assert res["summary"]["rows"] == ROWS

    t3 = time.perf_counter()
    r = client.patch(
        f"/api/v1/imports/{job['id']}/rows/5",
        json={"field": "email", "value": "edited@example.com"},
    )
    assert r.status_code == 200
    t_edit = time.perf_counter() - t3

    t4 = time.perf_counter()
    out = client.get(f"/api/v1/imports/{job['id']}/export?format=csv&scope=all")
    assert out.status_code == 200
    page = client.get(f"/api/v1/imports/{job['id']}/rows?page=100&page_size=100").json()
    assert len(page["rows"]) == 100
    t_export = time.perf_counter() - t4

    total = time.perf_counter() - t0
    print(
        f"\n10k x 50: upload {t_upload:.1f}s, map {t_map:.1f}s, validate {t_validate:.1f}s, "
        f"edit {t_edit:.2f}s, export+page {t_export:.1f}s, total {total:.1f}s"
    )
    assert total < TIME_BUDGET_S, f"pipeline too slow: {total:.1f}s"
    assert t_edit < 5.0, "single-row edit must not reprocess the whole file"
