"""Deterministic generator for the demo datasets.

    python demo-data/generate_demo_data.py

Creates three files with completely different headers that all import into
"Customer Import v1", each seeded with realistic problems (invalid emails,
duplicate IDs / emails, empty required names, mixed phone/date formats, country
spelled three ways, whitespace, N/A tokens). Counts are deterministic (seed 42).
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent
rng = random.Random(42)

FIRST = [
    "Dana",
    "Noa",
    "Yosef",
    "Amir",
    "Tamar",
    "Lior",
    "Maya",
    "Omer",
    "Shira",
    "Eitan",
    "Sarah",
    "David",
    "Michael",
    "Emma",
    "Olivia",
    "James",
    "Liam",
    "Sophia",
    "Ava",
    "Noah",
    "Ahmed",
    "Fatima",
    "Omar",
    "Layla",
    "Yusuf",
    "Hana",
    "Ali",
    "Sara",
    "Karim",
    "Nadia",
    "Ivan",
    "Anna",
    "Pavel",
    "Elena",
    "Dmitry",
    "Olga",
    "Sergey",
    "Maria",
    "Andrei",
    "Irina",
]
LAST = [
    "Levi",
    "Cohen",
    "Mizrahi",
    "Peretz",
    "Biton",
    "Friedman",
    "Katz",
    "Avraham",
    "Dahan",
    "Smith",
    "Johnson",
    "Brown",
    "Taylor",
    "Wilson",
    "Davies",
    "Evans",
    "Thomas",
    "Roberts",
    "Al Mansoori",
    "Al Maktoum",
    "Haddad",
    "Khalil",
    "Nasser",
    "Saleh",
    "Rahman",
    "Ivanov",
    "Petrov",
    "Sidorov",
    "Smirnova",
    "Kuznetsov",
    "Popova",
    "Volkov",
]
COMPANIES = [
    "Acme Ltd",
    "Globex",
    "Initech",
    "Umbrella Corp",
    "Hooli",
    "Vandelay Industries",
    "Stark Industries",
    "Wayne Enterprises",
    "Wonka Inc",
    "Soylent Corp",
    "Tyrell Corp",
    "Cyberdyne Systems",
    "Massive Dynamic",
    "Aperture Science",
    "Blue Sun",
    "Sirius Cybernetics",
    "Oceanic Airlines",
    "Gringotts",
    "Dunder Mifflin",
    "Prestige Worldwide",
]
DOMAINS = [
    "example.com",
    "mail.example.org",
    "corp.example.net",
    "acme.example",
    "test.example.com",
]

# country spellings: (display variants, ISO, phone generator)
COUNTRIES = {
    "IL": (
        ["Israel", "IL", "ISR", "israel"],
        lambda: f"05{rng.choice([0, 2, 3, 4])}{rng.randint(2000000, 9999999)}",
    ),
    "AE": (
        ["United Arab Emirates", "UAE", "AE", "ARE"],
        lambda: f"05{rng.choice([0, 2, 4, 5, 6])}{rng.randint(2000000, 9999999)}",
    ),
    "GB": (
        ["United Kingdom", "UK", "GB", "GBR"],
        lambda: f"07{rng.randint(700, 999)} {rng.randint(100000, 999999)}",
    ),
    "US": (
        ["United States", "USA", "US"],
        lambda: f"({rng.choice(US_AREA_CODES)}) {rng.randint(200, 999)}-{rng.randint(1000, 9999)}",
    ),
    "DE": (
        ["Germany", "DE", "DEU", "Deutschland"],
        lambda: f"0{rng.choice([170, 171, 175, 176])} {rng.randint(1000000, 9999999)}",
    ),
}
US_AREA_CODES = [212, 310, 415, 617, 702, 305, 206, 312, 646, 720]
_IL_CC = {"IL": "+972", "AE": "+971", "GB": "+44", "US": "+1", "DE": "+49"}


def phone_for(iso: str, style: str) -> str:
    local = COUNTRIES[iso][1]()
    if style == "intl":
        digits = "".join(ch for ch in local if ch.isdigit()).lstrip("0")
        return f"{_IL_CC[iso]}{digits}"
    if style == "intl_spaced":
        digits = "".join(ch for ch in local if ch.isdigit()).lstrip("0")
        return f"{_IL_CC[iso]} {digits[:2]} {digits[2:]}"
    if style == "dashed":
        digits = "".join(ch for ch in local if ch.isdigit())
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) >= 9 else local
    return local


def date_variants(y: int, m: int, d: int, style: str) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if style == "iso":
        return f"{y:04d}-{m:02d}-{d:02d}"
    if style == "dmy":
        return f"{d:02d}/{m:02d}/{y:04d}"
    if style == "dots":
        return f"{d:02d}.{m:02d}.{y:04d}"
    if style == "text":
        return f"{months[m - 1]} {d} {y}"
    return f"{d} {months[m - 1]} {y}"


def base_rows(n: int, prefix: str) -> list[dict]:
    rows = []
    for i in range(n):
        iso = rng.choices(list(COUNTRIES), weights=[45, 20, 15, 12, 8])[0]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        name = f"{first} {last}"
        email = f"{first}.{last}".lower().replace(" ", "") + f"{i}@{rng.choice(DOMAINS)}"
        rows.append(
            {
                "id": f"{prefix}-{1001 + i}",
                "name": name,
                "email": email,
                "iso": iso,
                "country": rng.choice(COUNTRIES[iso][0]),
                "phone": phone_for(iso, rng.choice(["local", "intl", "intl_spaced", "dashed"])),
                "company": rng.choice(COMPANIES),
                "date": (rng.randint(2022, 2026), rng.randint(1, 12), rng.randint(1, 28)),
                "date_style": rng.choice(["iso", "dmy", "dots", "text", "text2"]),
            }
        )
    return rows


def inject_problems(rows: list[dict], spec: dict) -> dict:
    """Mutate rows in place; return the counts actually injected."""
    n = len(rows)
    idx = list(range(n))
    rng.shuffle(idx)
    cursor = 0
    counts: dict[str, int] = {}

    def take(k: int) -> list[int]:
        nonlocal cursor
        picked = idx[cursor : cursor + k]
        cursor += k
        return picked

    for i in take(spec["invalid_email"]):
        rows[i]["email"] = rng.choice(
            [
                rows[i]["email"].replace("@", " at "),
                rows[i]["email"].replace(".", ""),
                "not-an-email",
                rows[i]["email"] + "@dup",
            ]
        )
    counts["invalid_email"] = spec["invalid_email"]
    for i in take(spec["duplicate_id"]):
        rows[i]["id"] = rows[(i + 7) % n]["id"]
    counts["duplicate_id"] = spec["duplicate_id"]
    for i in take(spec["duplicate_email"]):
        rows[i]["email"] = rows[(i + 11) % n]["email"]
    counts["duplicate_email"] = spec["duplicate_email"]
    for i in take(spec["empty_name"]):
        rows[i]["name"] = rng.choice(["", "N/A", "-", "null"])
    counts["empty_name"] = spec["empty_name"]
    for i in take(spec["bad_phone"]):
        rows[i]["phone"] = rng.choice(["05012ABC", "12345", "+972 5", "call me"])
    counts["bad_phone"] = spec["bad_phone"]
    for i in take(spec["typo_country"]):
        rows[i]["country"] = rng.choice(["Isreal", "Untied Kingdom", "Germny"])
    counts["typo_country"] = spec["typo_country"]
    for i in take(spec["whitespace"]):
        rows[i]["name"] = f"  {rows[i]['name']} "
        rows[i]["company"] = f"{rows[i]['company']}   "
    counts["whitespace"] = spec["whitespace"]
    for i in take(spec["na_company"]):
        rows[i]["company"] = rng.choice(["N/A", "null", "-", "NULL"])
    counts["na_company"] = spec["na_company"]
    for i in take(spec["bad_date"]):
        rows[i]["date_style"] = "broken"
    counts["bad_date"] = spec["bad_date"]
    return counts


def fmt_date(r: dict) -> str:
    y, m, d = r["date"]
    if r["date_style"] == "broken":
        return rng.choice(["31/02/2025", "yesterday", "2025-13-40"])
    return date_variants(y, m, d, r["date_style"])


def write_xlsx(
    path: Path,
    headers: list[str],
    rows: list[list],
    extra_sheet: tuple[str, list[list]] | None = None,
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Customers"
    ws.append(headers)
    for r in rows:
        ws.append(r)
        # keep "=..." payloads as literal text cells (like a malicious CSV converted to XLSX)
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    if extra_sheet:
        ws2 = wb.create_sheet(extra_sheet[0])
        for r in extra_sheet[1]:
            ws2.append(r)
    wb.save(path)


def main() -> None:
    # ---------------------------------------------------------------- acme-customers.xlsx
    acme = base_rows(500, "ACM")
    acme_counts = inject_problems(
        acme,
        {
            "invalid_email": 9,
            "duplicate_id": 6,
            "duplicate_email": 4,
            "empty_name": 5,
            "bad_phone": 12,
            "typo_country": 7,
            "whitespace": 25,
            "na_company": 10,
            "bad_date": 3,
        },
    )
    # a spreadsheet-formula payload in a mapped column: exports must neutralise it
    acme[3]["company"] = '=HYPERLINK("http://evil.example","click me")'
    write_xlsx(
        HERE / "acme-customers.xlsx",
        [
            "Customer Ref",
            "Client",
            "E-mail Address",
            "Mobile No.",
            "Organization",
            "Country Name",
            "Joined",
            "Notes",
        ],
        [
            [
                r["id"],
                r["name"],
                r["email"],
                r["phone"],
                r["company"],
                r["country"],
                fmt_date(r),
                rng.choice(["", "", "VIP", "Referred by partner"]),
            ]
            for r in acme
        ],
    )

    # ------------------------------------------------ legacy-crm.csv (semicolon, UTF-8 BOM)
    legacy = base_rows(300, "LEG")
    legacy_counts = inject_problems(
        legacy,
        {
            "invalid_email": 6,
            "duplicate_id": 0,
            "duplicate_email": 3,
            "empty_name": 4,
            "bad_phone": 8,
            "typo_country": 4,
            "whitespace": 15,
            "na_company": 6,
            "bad_date": 2,
        },
    )
    with open(HERE / "legacy-crm.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Full Name", "Mail", "Telephone", "Business", "Location", "Created"])
        for r in legacy:
            w.writerow([r["name"], r["email"], r["phone"], r["company"], r["country"], fmt_date(r)])

    # ------------------------------------------------ partner-export.xlsx (two sheets)
    partner = base_rows(400, "PRT")
    for r in partner:
        r["country"] = r["iso"]  # partner already sends ISO codes
        r["date_style"] = "iso"
    partner_counts = inject_problems(
        partner,
        {
            "invalid_email": 5,
            "duplicate_id": 0,
            "duplicate_email": 2,
            "empty_name": 3,
            "bad_phone": 6,
            "typo_country": 0,
            "whitespace": 12,
            "na_company": 5,
            "bad_date": 0,
        },
    )
    write_xlsx(
        HERE / "partner-export.xlsx",
        ["customer", "email_address", "phone_number", "company_name", "country_code", "created_at"],
        [
            [r["name"], r["email"], r["phone"], r["company"], r["country"], fmt_date(r)]
            for r in partner
        ],
        extra_sheet=(
            "Read me",
            [
                ["Partner export"],
                ["Generated for Magic Import demo"],
                ["Sheet 'Customers' holds the data"],
            ],
        ),
    )

    merchant_counts = write_merchant_onboarding(HERE / "merchant-onboarding.xlsx")

    print("acme-customers.xlsx", acme_counts)
    print("legacy-crm.csv", legacy_counts)
    print("partner-export.xlsx", partner_counts)
    print("merchant-onboarding.xlsx", merchant_counts)


# ------------------------------------------------------------ merchant-onboarding.xlsx
# Imports into the schema built from demo-data/merchant-api.yaml (POST /v1/merchants).
# Own RNG so the customer files above stay byte-for-byte reproducible.
MERCHANT_NAMES = [
    "Al Noor Trading",
    "Desert Rose Cafe",
    "Blue Lagoon Spa",
    "Falcon Electronics",
    "Palm Grove Bakery",
    "Oasis Pharmacy",
    "Gulf Star Motors",
    "Marina Fresh Market",
    "Zayed Books",
    "Silk Road Carpets",
    "Harbour Fish House",
    "Golden Dune Tours",
    "Crescent Tailors",
    "Sunrise Laundry",
    "Pearl Jewellers",
    "Cedar Grill",
    "Nomad Coffee",
    "Skyline Fitness",
    "Amber Florist",
    "Horizon Optics",
]
MERCHANT_CITIES = {
    "AE": (["Dubai", "Abu Dhabi", "Sharjah"], ["United Arab Emirates", "UAE", "AE"], "AED"),
    "SA": (["Riyadh", "Jeddah"], ["Saudi Arabia", "KSA", "SA"], "SAR"),
    "GB": (["London", "Manchester"], ["United Kingdom", "UK", "GB"], "GBP"),
}
STREETS = ["Al Wasl Rd", "King Fahd Rd", "High St", "Sheikh Zayed Rd", "Corniche St"]
MCCS = ["5812", "5411", "5999", "7230", "5732", "5912", "5541", "7997"]


def write_merchant_onboarding(path: Path) -> dict:
    mrng = random.Random(2026)
    rows = []
    for i in range(120):
        iso = mrng.choices(list(MERCHANT_CITIES), weights=[60, 25, 15])[0]
        cities, country_names, ccy = MERCHANT_CITIES[iso]
        base = mrng.choice(MERCHANT_NAMES)
        slug = base.lower().replace(" ", "")
        if iso == "AE":
            phone = (
                mrng.choice(["+97150", "050 ", "+971 55 "]) + f"{mrng.randint(1000000, 9999999)}"
            )
        elif iso == "SA":
            phone = f"+9665{mrng.randint(10000000, 99999999)}"
        else:
            phone = mrng.choice(["+447400", "07400 "]) + f"{mrng.randint(100000, 999999)}"
        y, m, d = mrng.randint(2023, 2026), mrng.randint(1, 12), mrng.randint(1, 28)
        rows.append(
            {
                "ref": f"MRC-{2001 + i}",
                "legal": f"{base} {mrng.choice(['LLC', 'FZ-LLC', 'Ltd', 'Trading LLC'])}",
                "trading": base,
                "email": f"finance{i}@{slug}.example",
                "phone": phone,
                "street": f"{mrng.randint(1, 250)} {mrng.choice(STREETS)}",
                "city": mrng.choice(cities),
                "postcode": "" if iso != "GB" else f"M{mrng.randint(1, 9)} {mrng.randint(1, 9)}AB",
                "country": mrng.choice(country_names),
                "mcc": mrng.choice(MCCS),
                "ccy": mrng.choice([ccy, ccy.lower(), ccy]),
                "volume": f"{mrng.randint(5, 900) * 100}.{mrng.choice(['00', '50', '75'])}",
                "tips": mrng.choice(["Yes", "No", "yes", "N", "Y"]),
                "risk": mrng.choice(["low", "Medium", "", "high"]),
                "onboarded": mrng.choice([f"{y:04d}-{m:02d}-{d:02d}", f"{d:02d}.{m:02d}.{y:04d}"]),
                "website": mrng.choice(["", f"https://{slug}.example", f"www.{slug}.example"]),
                "tags": mrng.choice(["", "retail", "food; delivery", "retail, premium"]),
            }
        )
    idx = list(range(len(rows)))
    mrng.shuffle(idx)
    counts = {"invalid_email": 3, "bad_mcc": 3, "missing_legal_name": 2, "missing_city": 2}
    counts.update({"duplicate_ref": 2, "negative_volume": 1, "unknown_currency": 2})
    cursor = 0
    for key, n in counts.items():
        for i in idx[cursor : cursor + n]:
            r = rows[i]
            if key == "invalid_email":
                r["email"] = r["email"].replace("@", " at ")
            elif key == "bad_mcc":
                r["mcc"] = mrng.choice(["581", "58 12", "MCC5812"])
            elif key == "missing_legal_name":
                r["legal"] = mrng.choice(["", "N/A"])
            elif key == "missing_city":
                r["city"] = ""
            elif key == "duplicate_ref":
                r["ref"] = rows[(i + 5) % len(rows)]["ref"]
            elif key == "negative_volume":
                r["volume"] = "-1200.00"
            elif key == "unknown_currency":
                r["ccy"] = "dirhams"
        cursor += n
    headers = [
        "Merchant Ref",
        "Legal Name",
        "Trading As",
        "Contact Email",
        "Phone",
        "Street",
        "City",
        "Postcode",
        "Country",
        "MCC",
        "Settlement Currency",
        "Monthly Volume",
        "Accepts Tips",
        "Risk Level",
        "Onboarded",
        "Website",
        "Tags",
    ]
    keys = ["ref", "legal", "trading", "email", "phone", "street", "city", "postcode"]
    keys += ["country", "mcc", "ccy", "volume", "tips", "risk", "onboarded", "website", "tags"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Merchants"
    ws.append(headers)
    for r in rows:
        ws.append([r[k] for k in keys])
    wb.save(path)
    return counts


if __name__ == "__main__":
    main()
