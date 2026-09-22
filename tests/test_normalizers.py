import pytest

from app.services import normalizers as norm


def test_trim_and_null():
    assert norm.trim("  John   Smith ") == "John Smith"
    assert norm.trim("   ") is None
    for token in ["N/A", "-", "null", "NULL", "none", "—", ""]:
        assert norm.normalize_null(token) is None
    assert norm.normalize_null("keep") == "keep"
    assert norm.normalize_null("TBD", {"tbd"}) is None


def test_case():
    assert norm.apply_case("john smith", "title") == "John Smith"
    assert norm.apply_case("john", "upper") == "JOHN"
    assert norm.apply_case("JOHN", "lower") == "john"


def test_email():
    assert norm.normalize_email(" Dana@Example.COM ").value == "Dana@example.com"  # domain only
    assert norm.normalize_email("not-an-email").warning == "email_invalid"
    assert norm.normalize_email("a b@example.com").warning == "email_invalid"
    assert norm.normalize_email("dana@example.com@dup").warning == "email_invalid"


@pytest.mark.parametrize(
    "raw,region,expected",
    [
        ("052-555-1234", "IL", "+972525551234"),
        ("052 555 1234", "IL", "+972525551234"),
        ("+972 52 5551234", None, "+972525551234"),
        ("+972525551234", "US", "+972525551234"),  # explicit country code wins
        ("0525551234", "Israel", "+972525551234"),  # country name as region context
        ("(212) 555-0123", "US", "+12125550123"),
        ("+44 7911 123456", None, "+447911123456"),
    ],
)
def test_phone_ok(raw, region, expected):
    r = norm.normalize_phone(raw, region)
    assert r.ok and r.value == expected


def test_phone_doubtful_never_silently_fixed():
    assert norm.normalize_phone("05012ABC", "IL").warning == "phone_invalid"
    assert norm.normalize_phone("12345", "IL").warning == "phone_invalid"
    r = norm.normalize_phone("0525551234", None)  # no region -> cannot be resolved
    assert r.warning == "phone_unparseable" and r.value == "0525551234"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Israel", "IL"),
        ("IL", "IL"),
        ("ISR", "IL"),
        ("ישראל", "IL"),
        ("israel", "IL"),
        ("United Kingdom", "GB"),
        ("UK", "GB"),
        ("USA", "US"),
        ("United States", "US"),
        ("UAE", "AE"),
        ("Deutschland", "DE"),
        ("Россия", "RU"),
        ("Czech Republic", "CZ"),
    ],
)
def test_country(raw, expected):
    r = norm.normalize_country(raw)
    assert r.ok and r.value == expected


def test_country_unknown_and_suggestion():
    r = norm.normalize_country("Isreal")
    assert r.warning == "country_unknown" and r.value == "Isreal"
    assert norm.country_suggestion("Isreal") == "IL"
    assert norm.country_suggestion("Untied Kingdom") == "GB"
    assert norm.country_suggestion("xq") is None


def test_currency():
    assert norm.normalize_currency("USD").value == "USD"
    assert norm.normalize_currency("usd").value == "USD"
    assert norm.normalize_currency("US Dollar").value == "USD"
    assert norm.normalize_currency("€").value == "EUR"
    assert norm.normalize_currency("₪").value == "ILS"
    assert norm.normalize_currency("$").warning == "currency_ambiguous"
    assert norm.normalize_currency("Zorkmid").warning == "currency_unknown"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Yes", "true"),
        ("no", "false"),
        ("Y", "true"),
        ("N", "false"),
        ("1", "true"),
        ("0", "false"),
        ("TRUE", "true"),
        ("false", "false"),
    ],
)
def test_boolean(raw, expected):
    assert norm.normalize_boolean(raw).value == expected


def test_boolean_invalid():
    assert norm.normalize_boolean("maybe").warning == "boolean_invalid"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,299.50", "1299.50"),
        ("1299,50", "1299.50"),
        ("₪1,299.50", "1299.50"),
        ("1.299,50", "1299.50"),
        ("$ 12", "12"),
        ("(100.00)", "-100.00"),
        ("-5", "-5"),
        ("1 234,5", "1234.5"),
        ("0.1", "0.1"),
        ("1,234,567", "1234567"),
    ],
)
def test_decimal(raw, expected):
    r = norm.normalize_decimal(raw)
    assert r.ok, r
    assert r.value == expected


def test_decimal_is_exact_not_float():
    from decimal import Decimal

    assert Decimal(norm.normalize_decimal("0.1").value) + Decimal(
        norm.normalize_decimal("0.2").value
    ) == Decimal("0.3")


def test_decimal_ambiguous_and_invalid():
    assert norm.normalize_decimal("1,299").warning == "decimal_ambiguous"
    assert norm.normalize_decimal("abc").warning == "decimal_invalid"
    assert norm.normalize_decimal("1.2.3").warning == "decimal_invalid"


def test_integer():
    assert norm.normalize_integer("42").value == "42"
    assert norm.normalize_integer("42.0").value == "42"
    assert norm.normalize_integer("4 2").value == "42"
    assert norm.normalize_integer("4.2").warning == "integer_invalid"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("21/09/2026", "2026-09-21"),
        ("2026-09-21", "2026-09-21"),
        ("Sep 21 2026", "2026-09-21"),
        ("21 Sep 2026", "2026-09-21"),
        ("21.09.2026", "2026-09-21"),
        ("September 21, 2026", "2026-09-21"),
        ("2026-09-21 13:45:00", "2026-09-21"),
    ],
)
def test_date(raw, expected):
    r = norm.normalize_date(raw)
    assert r.value == expected and r.ok


def test_date_ambiguity_is_flagged_not_guessed_silently():
    r = norm.normalize_date("03/04/2026")
    assert r.value == "2026-04-03" and r.warning == "date_ambiguous"
    assert norm.normalize_date("03/04/2026", dayfirst=False).value == "2026-03-04"
    assert norm.normalize_date("03/04/2026", dayfirst=True).ok
    assert norm.normalize_date("13/04/2026").ok  # unambiguous
    assert norm.normalize_date("31/02/2025").warning == "date_invalid"
    assert norm.normalize_date("yesterday").warning == "date_invalid"


def test_datetime_and_url():
    assert norm.normalize_datetime("2026-09-21T13:45:10Z").value.startswith("2026-09-21 13:45:10")
    assert norm.normalize_datetime("nope").warning == "datetime_invalid"
    assert norm.normalize_url("example.com/x").value == "https://example.com/x"
    assert norm.normalize_url("not a url").warning == "url_invalid"
