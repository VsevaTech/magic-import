"""End-to-end browser demo + screenshot capture (Playwright).

    python scripts/browser_demo.py --base http://127.0.0.1:8000 --out docs/screenshots

Walks the real UI through the whole flow with the three demo files, asserts the key
outcomes, and saves the screenshots used in the README. Exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo-data"


def shot(page: Page, out: Path, name: str, full: bool = False) -> None:
    page.wait_for_timeout(450)  # let fade-in animations settle
    page.screenshot(path=str(out / f"{name}.png"), full_page=full)
    print(f"  📸 {name}.png")


def step_url(page: Page) -> int:
    m = re.search(r"step=(\d)", page.url)
    return int(m.group(1)) if m else 0


def wait_alpine(page: Page) -> None:
    """Wait until Alpine has initialised the page (its handlers are attached only then)."""
    page.wait_for_function(
        "() => window.Alpine && Array.from(document.querySelectorAll('[x-data]'))"
        ".every(el => el._x_dataStack !== undefined)"
    )


def run_import(
    page: Page,
    out: Path,
    filename: str,
    prefix: str,
    with_shots: bool,
    schema: str = "Customer Import v1",
) -> dict:
    """with_shots=True captures every step; a prefix captures only the mapping step."""
    print(f"\n▶ {filename}")
    page.goto("/imports/new")
    wait_alpine(page)
    if with_shots:
        shot(page, out, "02-upload")
    page.set_input_files("input[type=file]", str(DEMO / filename))
    page.wait_for_url(re.compile(r"/imports/[0-9a-f]{32}$"), timeout=30_000)
    job_id = page.url.rsplit("/", 1)[-1]
    print(f"  job {job_id}")

    # ---- step 2: inspect
    expect(page.get_by_text("File inspection")).to_be_visible()
    if with_shots:
        shot(page, out, "03-inspect", full=True)
    page.get_by_label(schema).check()
    page.get_by_role("button", name=re.compile("Continue to mapping")).click()
    page.wait_for_url(re.compile(r"step=3"))

    # ---- step 3: map
    expect(page.get_by_text("Map columns to")).to_be_visible()
    page.wait_for_selector("table tbody tr")
    rows = page.locator("table tbody tr")
    n = rows.count()
    highs = page.locator("table tbody .badge-high").count()
    print(f"  mapping: {n} columns, {highs} HIGH")
    assert highs >= n - 1, "expected (almost) all columns to map with HIGH confidence"
    if prefix:
        shot(page, out, f"{prefix}-mapping", full=True)
    elif with_shots:
        shot(page, out, "04-mapping", full=True)
    page.get_by_role("button", name=re.compile("Confirm mapping")).click()
    page.wait_for_url(re.compile(r"step=4"))

    # ---- step 4: transform
    expect(page.get_by_text("Transformations").first).to_be_visible()
    page.wait_for_selector("li .toggle")
    labels = page.locator("li .toggle + div p.font-medium").all_inner_texts()
    print("  transformations:", "; ".join(labels))
    assert any("Phone" in x for x in labels) and any("Country" in x for x in labels)
    if schema != "Customer Import v1":
        assert any("Currency" in x for x in labels) and any("Decimal" in x for x in labels)
    if with_shots:
        shot(page, out, "05-transform", full=True)
    page.get_by_role("button", name=re.compile("Run validation")).click()
    page.wait_for_url(re.compile(r"step=5"), timeout=60_000)

    # ---- step 5: validate
    expect(page.get_by_text("Data quality")).to_be_visible()
    page.wait_for_selector("text=/Import-ready/")
    page.wait_for_timeout(600)
    stats = {}
    for label in ("Ready", "Warnings", "Errors"):
        el = page.locator(f"a.stat:has(div.stat-label:text-is('{label}')) .stat-value")
        stats[label.lower()] = int(el.inner_text().replace(",", ""))
    print("  summary:", stats)
    if with_shots:
        shot(page, out, "06-validation", full=True)
    return {"job_id": job_id, "stats": stats}


_STATE: dict = {}  # current page / output dir, for failure diagnostics


def main() -> int:
    try:
        return _main()
    except Exception as exc:  # print where we were so CI logs are actionable
        page = _STATE.get("page")
        print(f"\n❌ browser demo failed: {type(exc).__name__}: {exc}")
        if page is not None:
            try:
                print(f"   url: {page.url}")
                out = Path(_STATE.get("out", "."))
                page.screenshot(path=str(out / "zz-failure.png"), full_page=True)
                print("   screenshot: zz-failure.png")
            except Exception:  # pragma: no cover - best effort
                pass
        return 1


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        if args.chromium:
            launch["executable_path"] = args.chromium
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(
            base_url=args.base,
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.set_default_timeout(45_000)
        _STATE.update(page=page, out=out)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 1) main demo file --------------------------------------------------
        acme = run_import(page, out, "acme-customers.xlsx", "", with_shots=True)
        job = acme["job_id"]

        # bulk fix from the data quality page
        apply_btns = page.get_by_role("button", name=re.compile(r"^Apply to \d+"))
        assert apply_btns.count() >= 1, "expected bulk-fix suggestions"
        first_label = page.locator("li:has(button:text-matches('Apply to')) p").first.inner_text()
        apply_btns.first.click()
        page.wait_for_selector("text=/Applied to/")
        print(f"  bulk fix applied: {first_label}")

        # 2) review: inline edit ---------------------------------------------
        page.goto(f"/imports/{job}?step=6&status=error")
        page.wait_for_selector("table tbody tr")
        page.wait_for_timeout(500)
        err_cells = page.locator("button.cell-error")
        assert err_cells.count() > 0
        first = err_cells.first
        first.click()
        page.wait_for_selector("text=/Row \\d+ ·/")
        shot(page, out, "07-review-cell")
        page.keyboard.press("Escape")
        # find an invalid email cell to fix
        page.goto(f"/imports/{job}?step=6&code=email_invalid")
        page.wait_for_selector("table tbody tr")
        page.wait_for_timeout(500)
        cell = page.locator("button.cell-error").first
        before = cell.inner_text()
        cell.dblclick()
        page.keyboard.press("Control+A")
        page.keyboard.type("fixed.by.demo@example.com")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=/revalidated/")
        print(f"  inline edit: {before!r} → fixed.by.demo@example.com")
        page.goto(f"/imports/{job}?step=6")
        page.wait_for_selector("table tbody tr")
        shot(page, out, "07-review", full=False)
        # toggle original view
        page.get_by_role("button", name="Original", exact=True).click()
        page.wait_for_timeout(300)
        # undo last change
        page.get_by_role("button", name=re.compile("Undo")).click()
        page.wait_for_selector("text=/Undid:/")
        print("  undo OK")
        # save mapping template
        page.get_by_role("button", name="Save mapping template").click()
        page.get_by_placeholder("Acme CRM Customer Export").fill("Acme CRM Customer Export")
        page.get_by_role("button", name="Save template").click()
        page.wait_for_selector("text=/Template .* saved/")
        print("  template saved")

        # 3) export -----------------------------------------------------------
        page.goto(f"/imports/{job}?step=7")
        page.wait_for_selector("text=Import report")
        page.wait_for_timeout(500)
        shot(page, out, "08-export", full=True)
        with page.expect_download() as dl:
            page.get_by_role("button", name=re.compile("Excel workbook")).click()
        d = dl.value
        target = out.parent / "demo-exports"
        target.mkdir(exist_ok=True)
        d.save_as(str(target / d.suggested_filename))
        print(f"  downloaded {d.suggested_filename}")
        with page.expect_download() as dl:
            page.get_by_role("button", name="Download errors.csv").click()
        dl.value.save_as(str(target / dl.value.suggested_filename))
        print(f"  downloaded {dl.value.suggested_filename}")
        page.get_by_role("button", name="Mark import as completed").click()
        page.wait_for_selector("text=Completed")

        # 4) second file: totally different headers, same schema ---------------
        legacy = run_import(page, out, "legacy-crm.csv", "09-legacy", with_shots=False)
        # 5) third file: multi-sheet xlsx ------------------------------------------
        page.goto("/imports/new")
        wait_alpine(page)
        page.set_input_files("input[type=file]", str(DEMO / "partner-export.xlsx"))
        page.wait_for_url(re.compile(r"/imports/[0-9a-f]{32}$"))
        expect(page.get_by_text("Choose a sheet")).to_be_visible()
        page.get_by_role("button", name="Read me").click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)
        assert "Read me" in page.locator("h1 + p").inner_text()
        page.get_by_role("button", name="Customers", exact=True).click()
        page.wait_for_load_state("networkidle")
        print("  sheet switching OK")
        page.get_by_label("Customer Import v1").check()
        page.get_by_role("button", name=re.compile("Continue to mapping")).click()
        page.wait_for_url(re.compile(r"step=3"))
        page.wait_for_selector("table tbody tr")
        assert page.locator("table tbody .badge-high").count() == 6
        print("  partner-export mapped: 6 HIGH")

        # 6) schema builder + dashboard ---------------------------------------------
        page.goto("/schemas/new")
        page.wait_for_selector("text=Schema Builder")
        shot(page, out, "10-schema-builder", full=True)
        page.goto("/schemas")
        page.wait_for_selector("text=Mapping templates")
        assert page.get_by_text("Acme CRM Customer Export").count() >= 1
        page.goto("/")
        page.wait_for_selector("text=Recent Imports")
        shot(page, out, "01-dashboard", full=True)
        page.goto("/docs")
        try:  # Swagger UI loads from a CDN; skip the screenshot when offline
            page.wait_for_selector("text=Magic Import API", timeout=8_000)
            page.wait_for_timeout(800)
            shot(page, out, "11-openapi")
        except Exception:
            print("  (skipped OpenAPI screenshot: Swagger UI assets not reachable)")

        # 7) schema from an API contract → import → verified payloads -----------------
        print("\n▶ schema from OpenAPI (merchant-api.yaml → POST /v1/merchants)")
        page.goto("/schemas")
        wait_alpine(page)
        page.get_by_test_id("from-openapi").click()
        page.wait_for_url(re.compile(r"/schemas/from-contract$"))
        wait_alpine(page)
        page.get_by_test_id("contract-sample").click()
        targets = page.get_by_test_id("contract-targets")
        expect(targets.get_by_text("/v1/merchants", exact=True)).to_be_visible()
        print("  operations:", targets.locator("li button").count())
        shot(page, out, "13-openapi-targets", full=True)
        targets.get_by_text("/v1/merchants", exact=True).click()
        fields = page.get_by_test_id("contract-fields")
        expect(fields.get_by_text("address.city", exact=True)).to_be_visible()
        n_fields = fields.locator("tbody tr").count()
        skipped = page.get_by_test_id("contract-skipped").inner_text()
        print(f"  preview: {n_fields} fields; skipped: {' / '.join(skipped.splitlines()[::2])}")
        assert n_fields == 18 and "owners" in skipped and "id" in skipped
        shot(page, out, "14-openapi-fields", full=True)
        schema_name = page.get_by_test_id("contract-name").input_value()
        page.get_by_test_id("contract-create").click()
        page.wait_for_url(re.compile(r"/schemas\?created="))
        expect(page.get_by_text("API contract").first).to_be_visible()
        print(f"  created schema {schema_name!r}")

        merchants = run_import(
            page,
            out,
            "merchant-onboarding.xlsx",
            "15-openapi",
            with_shots=False,
            schema=schema_name,
        )
        mjob = merchants["job_id"]
        assert merchants["stats"] == {"ready": 100, "warnings": 4, "errors": 16}, merchants
        page.goto(f"/imports/{mjob}?step=7")
        wait_alpine(page)
        page.get_by_test_id("contract-check").click()
        result = page.get_by_test_id("contract-result")
        expect(result).to_contain_text("All 104 payloads match POST /v1/merchants")
        print("  contract check:", " · ".join(result.inner_text().split("\n")).strip(" ·✓"))
        page.evaluate("window.scrollTo(0, 0)")
        shot(page, out, "16-contract-check", full=True)
        with page.expect_download() as dl:
            page.get_by_test_id("payload-download").click()
        dl.value.save_as(str(target / dl.value.suggested_filename))
        print(f"  downloaded {dl.value.suggested_filename}")

        # mobile check
        mobile = browser.new_context(
            base_url=args.base, viewport={"width": 390, "height": 844}, device_scale_factor=2
        )
        mp = mobile.new_page()
        mp.goto(f"/imports/{job}?step=5")
        mp.wait_for_selector("text=Data quality")
        mp.wait_for_timeout(600)
        mp.screenshot(path=str(out / "12-mobile.png"))
        print("  📸 12-mobile.png")

        browser.close()
        ignore = (
            "favicon",
            "ERR_TUNNEL_CONNECTION_FAILED",
            "SwaggerUIBundle",
            "ERR_NAME_NOT_RESOLVED",
        )
        real_errors = [e for e in errors if not any(x in e for x in ignore)]
        if real_errors:
            print("\nJS console errors:")
            for e in real_errors:
                print("  ", e)
            return 1
        print("\n✅ browser demo passed", {"acme": acme["stats"], "legacy": legacy["stats"]})
        return 0


if __name__ == "__main__":
    t = time.time()
    code = main()
    print(f"({time.time() - t:.1f}s)")
    sys.exit(code)
