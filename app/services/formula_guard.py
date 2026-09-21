"""Protection against spreadsheet formula injection in exported files.

A cell whose text starts with ``=``, ``+``, ``-``, ``@`` (or a tab / carriage return)
is interpreted as a formula by Excel, LibreOffice and Google Sheets when a CSV is
opened - a classic way to exfiltrate data (``=HYPERLINK(...)``) or run DDE commands.

Strategy (OWASP recommendation): prefix such values with a single quote so the
spreadsheet treats them as text. Legitimate numbers such as ``-5`` or ``+1.5`` are
left alone - only non-numeric strings are escaped.

For XLSX we additionally write every cell with an explicit *string* data type, so
openpyxl never stores them as formulas even if the guard was bypassed.
"""

from __future__ import annotations

import re

DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+([.,]\d+)?|[.,]\d+)([eE][+-]?\d+)?$")
_PHONE_RE = re.compile(r"^\+\d[\d\s\-()]{5,}$")


def is_dangerous(value: str) -> bool:
    if not value:
        return False
    if not value.startswith(DANGEROUS_PREFIXES):
        return False
    if _NUMERIC_RE.match(value.strip()):
        return False
    # "+972501234567" is a phone number, not a formula
    return not _PHONE_RE.match(value.strip())


def sanitize_cell(value):
    """Return a spreadsheet-safe representation of ``value``."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if is_dangerous(value):
        return "'" + value
    return value


def sanitize_row(row: dict) -> dict:
    return {k: sanitize_cell(v) for k, v in row.items()}
