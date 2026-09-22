"""Value-level normalizers. Each returns ``NormResult(value, warning)``.

A normalizer never silently turns a doubtful input into a "valid" output: when a value
is ambiguous it is returned unchanged together with a warning code so the validation
engine can surface it as a WARNING / ERROR for the user to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import phonenumbers
import pycountry
from dateutil import parser as dateparser
from email_validator import EmailNotValidError, validate_email

NULL_TOKENS = {"", "n/a", "na", "null", "none", "nil", "-", "--", "—", "?", "#n/a", "nan"}


@dataclass(frozen=True)
class NormResult:
    value: str | None
    warning: str | None = None  # machine code, e.g. "phone_unparseable"

    @property
    def ok(self) -> bool:
        return self.warning is None


def normalize_null(value: str | None, extra_tokens: set[str] | None = None) -> str | None:
    if value is None:
        return None
    tokens = NULL_TOKENS | {t.lower() for t in (extra_tokens or set())}
    return None if value.strip().lower() in tokens else value


def trim(value: str | None) -> str | None:
    if value is None:
        return None
    v = re.sub(r"\s+", " ", value.strip())
    return v if v != "" else None


def apply_case(value: str | None, mode: str) -> str | None:
    if value is None:
        return None
    if mode == "upper":
        return value.upper()
    if mode == "lower":
        return value.lower()
    if mode == "title":
        return " ".join(w[:1].upper() + w[1:].lower() for w in value.split(" "))
    return value


# ------------------------------------------------------------------ email
def normalize_email(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    v = value.strip()
    if "@" not in v:
        return NormResult(v, "email_invalid")
    local, _, domain = v.rpartition("@")
    v = f"{local}@{domain.lower()}"
    try:
        validate_email(v, check_deliverability=False)
    except EmailNotValidError:
        return NormResult(v, "email_invalid")
    return NormResult(v)


# ------------------------------------------------------------------ phone
def normalize_phone(value: str | None, default_region: str | None = None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip()
    digits = sum(c.isdigit() for c in raw)
    if digits < 6 or re.search(r"[A-Za-z]", raw):
        return NormResult(raw, "phone_invalid")
    region = (default_region or "").strip() or None
    if region and len(region) != 2:
        region = normalize_country(region).value
    if region:
        region = region.upper()
        if len(region) != 2:
            region = None
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return NormResult(raw, "phone_unparseable")
    if not phonenumbers.is_possible_number(parsed):
        return NormResult(raw, "phone_unparseable")
    if not phonenumbers.is_valid_number(parsed):
        # Possible but not valid for its region: keep original, ask the user.
        return NormResult(raw, "phone_unparseable")
    return NormResult(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))


def phone_region(value: str | None) -> str | None:
    """Region code of an already-normalized E.164 phone number (None if unknown)."""
    if not value:
        return None
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return None
    return phonenumbers.region_code_for_number(parsed)


# ------------------------------------------------------------------ country
_COUNTRY_EXTRA = {
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "america": "US",
    "united states of america": "US",
    "uae": "AE",
    "u.a.e.": "AE",
    "emirates": "AE",
    "russia": "RU",
    "россия": "RU",
    "ישראל": "IL",
    "israel": "IL",
    "holland": "NL",
    "the netherlands": "NL",
    "south korea": "KR",
    "korea": "KR",
    "czechia": "CZ",
    "czech republic": "CZ",
    "türkiye": "TR",
    "turkey": "TR",
    "vietnam": "VN",
    "iran": "IR",
    "syria": "SY",
    "laos": "LA",
    "bolivia": "BO",
    "venezuela": "VE",
    "tanzania": "TZ",
    "moldova": "MD",
    "macedonia": "MK",
    "north macedonia": "MK",
    "ivory coast": "CI",
    "palestine": "PS",
    "kosovo": "XK",
    "hong kong": "HK",
    "macau": "MO",
    "taiwan": "TW",
    "brunei": "BN",
    "cape verde": "CV",
    "swaziland": "SZ",
    "burma": "MM",
    "myanmar": "MM",
    "deutschland": "DE",
    "españa": "ES",
    "italia": "IT",
    "österreich": "AT",
    "schweiz": "CH",
    "polska": "PL",
    "sverige": "SE",
    "norge": "NO",
    "danmark": "DK",
    "suomi": "FI",
    "eesti": "EE",
    "latvija": "LV",
    "lietuva": "LT",
    "magyarország": "HU",
    "românia": "RO",
    "ελλάδα": "GR",
    "україна": "UA",
    "беларусь": "BY",
    "қазақстан": "KZ",
    "казахстан": "KZ",
    "мексика": "MX",
    "brasil": "BR",
    "nederland": "NL",
    "belgië": "BE",
    "belgique": "BE",
    "الإمارات": "AE",
    "مصر": "EG",
    "السعودية": "SA",
    "saudi arabia": "SA",
    "ksa": "SA",
}


def _lookup_country(candidate: str):
    up = candidate.upper()
    if len(up) == 2:
        return pycountry.countries.get(alpha_2=up)
    if len(up) == 3:
        c = pycountry.countries.get(alpha_3=up)
        if c:
            return c
    c = pycountry.countries.get(name=candidate)
    if c:
        return c
    c = pycountry.countries.get(official_name=candidate)
    if c:
        return c
    c = pycountry.countries.get(common_name=candidate)
    if c:
        return c
    return None


def normalize_country(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip()
    low = raw.lower().strip(". ")
    if low in _COUNTRY_EXTRA:
        return NormResult(_COUNTRY_EXTRA[low])
    hit = _lookup_country(raw)
    if hit:
        return NormResult(hit.alpha_2)
    # title-case lookup ("israel" -> "Israel")
    hit = _lookup_country(raw.title())
    if hit:
        return NormResult(hit.alpha_2)
    try:
        results = pycountry.countries.search_fuzzy(raw)
    except LookupError:
        results = []
    if len(results) == 1 and len(raw) >= 4:
        return NormResult(results[0].alpha_2)
    return NormResult(raw, "country_unknown")


_COUNTRY_NAMES: dict[str, str] | None = None


def _country_name_index() -> dict[str, str]:
    global _COUNTRY_NAMES
    if _COUNTRY_NAMES is None:
        index: dict[str, str] = {}
        for c in pycountry.countries:
            index[c.name.lower()] = c.alpha_2
            for attr in ("common_name", "official_name"):
                name = getattr(c, attr, None)
                if name:
                    index[name.lower()] = c.alpha_2
        for k, v in _COUNTRY_EXTRA.items():
            index[k] = v
        _COUNTRY_NAMES = index
    return _COUNTRY_NAMES


def country_suggestion(value: str, min_score: int = 80) -> str | None:
    """Best-effort suggestion for a misspelled country (used by bulk fixes)."""
    from rapidfuzz import fuzz, process

    if not value or len(value) < 3:
        return None
    index = _country_name_index()
    hit = process.extractOne(value.lower(), list(index.keys()), scorer=fuzz.WRatio)
    if hit and hit[1] >= min_score:
        return index[hit[0]]
    return None


# ------------------------------------------------------------------ currency
_CURRENCY_SYMBOLS = {
    "€": "EUR",
    "£": "GBP",
    "₪": "ILS",
    "¥": None,  # JPY or CNY - ambiguous
    "$": None,  # USD, CAD, AUD... - ambiguous
    "₽": "RUB",
    "₹": "INR",
    "₩": "KRW",
    "₺": "TRY",
    "₴": "UAH",
    "د.إ": "AED",
    "aed": "AED",
    "dirham": "AED",
    "us dollar": "USD",
    "us dollars": "USD",
    "dollar": None,
    "euro": "EUR",
    "euros": "EUR",
    "pound": "GBP",
    "pounds": "GBP",
    "sterling": "GBP",
    "shekel": "ILS",
    "shekels": "ILS",
    "nis": "ILS",
    "ruble": "RUB",
    "rouble": "RUB",
    "yen": "JPY",
    "yuan": "CNY",
    "rmb": "CNY",
}


def normalize_currency(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip()
    low = raw.lower()
    if low in _CURRENCY_SYMBOLS:
        code = _CURRENCY_SYMBOLS[low]
        return NormResult(code) if code else NormResult(raw, "currency_ambiguous")
    up = raw.upper()
    if len(up) == 3 and pycountry.currencies.get(alpha_3=up):
        return NormResult(up)
    hit = pycountry.currencies.get(name=raw.title())
    if hit:
        return NormResult(hit.alpha_3)
    return NormResult(raw, "currency_unknown")


# ------------------------------------------------------------------ boolean
_TRUE = {"true", "yes", "y", "1", "t", "on", "да"}
_FALSE = {"false", "no", "n", "0", "f", "off", "нет"}


def normalize_boolean(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    low = value.strip().lower()
    if low in _TRUE:
        return NormResult("true")
    if low in _FALSE:
        return NormResult("false")
    return NormResult(value, "boolean_invalid")


# ------------------------------------------------------------------ numbers
_CURRENCY_PREFIX_RE = re.compile(r"^[^\d\-+(]+|[^\d)]+$")


def normalize_decimal(value: str | None) -> NormResult:
    """Parse a decimal without binary floats. Ambiguous thousands/decimal separators
    (e.g. "1,299" alone) are flagged instead of guessed."""
    if value is None:
        return NormResult(None)
    raw = value.strip()
    s = _CURRENCY_PREFIX_RE.sub("", raw).replace(" ", "").replace(" ", "")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    if s.startswith("-"):
        negative = True
        s = s[1:]
    s = s.lstrip("+")
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return NormResult(raw, "decimal_invalid")
    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # the last separator is the decimal separator
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            # "1,299" - thousands separator or decimal? ambiguous.
            return NormResult(raw, "decimal_ambiguous")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif has_dot:
        parts = s.split(".")
        if len(parts) > 2:
            if all(len(p) == 3 for p in parts[1:]):
                s = s.replace(".", "")
            else:
                return NormResult(raw, "decimal_invalid")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return NormResult(raw, "decimal_invalid")
    if negative:
        d = -d
    return NormResult(format(d, "f"))  # keep the scale as written: "1,299.50" -> "1299.50"


def normalize_integer(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip().replace(" ", "")
    if re.fullmatch(r"[+-]?\d+", raw):
        return NormResult(str(int(raw)))
    if re.fullmatch(r"[+-]?\d+\.0+", raw):
        return NormResult(str(int(Decimal(raw))))
    return NormResult(value, "integer_invalid")


# ------------------------------------------------------------------ dates
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLASHED_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$")


def normalize_date(value: str | None, dayfirst: bool | None = None) -> NormResult:
    """Return ISO ``YYYY-MM-DD``.

    ``dayfirst`` may be forced; when unset a slashed date such as 03/04/2026 is only
    accepted if it is unambiguous (one part > 12), otherwise day-first is assumed
    and a warning is returned so the user can confirm.
    """
    if value is None:
        return NormResult(None)
    raw = value.strip()
    if _ISO_DATE_RE.match(raw):
        try:
            return NormResult(date.fromisoformat(raw).isoformat())
        except ValueError:
            return NormResult(raw, "date_invalid")
    warning = None
    m = _SLASHED_RE.match(raw)
    use_dayfirst = bool(dayfirst)
    if m and dayfirst is None:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            use_dayfirst = True
        elif b > 12 and a <= 12:
            use_dayfirst = False
        else:
            use_dayfirst = True
            if a != b and a <= 12 and b <= 12:
                warning = "date_ambiguous"
    try:
        parsed = dateparser.parse(raw, dayfirst=use_dayfirst, yearfirst=False)
    except (ValueError, OverflowError, TypeError):
        return NormResult(raw, "date_invalid")
    if parsed is None:
        return NormResult(raw, "date_invalid")
    return NormResult(parsed.date().isoformat(), warning)


def normalize_datetime(value: str | None, dayfirst: bool | None = None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip()
    try:
        parsed = dateparser.parse(raw, dayfirst=bool(dayfirst))
    except (ValueError, OverflowError, TypeError):
        return NormResult(raw, "datetime_invalid")
    if parsed is None:
        return NormResult(raw, "datetime_invalid")
    if isinstance(parsed, datetime):
        return NormResult(parsed.replace(microsecond=0).isoformat(sep=" "))
    return NormResult(raw, "datetime_invalid")


# ------------------------------------------------------------------ url
_URL_RE = re.compile(r"^(https?://)?([\w-]+\.)+[\w-]{2,}(/\S*)?$", re.I)


def normalize_url(value: str | None) -> NormResult:
    if value is None:
        return NormResult(None)
    raw = value.strip()
    if not _URL_RE.match(raw):
        return NormResult(raw, "url_invalid")
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    return NormResult(raw)


TYPE_NORMALIZERS = {
    "email": normalize_email,
    "phone": normalize_phone,
    "country": normalize_country,
    "currency": normalize_currency,
    "boolean": normalize_boolean,
    "decimal": normalize_decimal,
    "integer": normalize_integer,
    "date": normalize_date,
    "datetime": normalize_datetime,
    "url": normalize_url,
}
