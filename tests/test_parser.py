import pytest

from app.services import file_parser as fp
from app.services.errors import InvalidFileError
from tests.conftest import make_csv, make_xlsx

ROWS = [["Dana Levi", "dana@example.com", "052-555-1234"], ["Noa Katz", "noa@example.com", ""]]
HEADERS = ["Name", "Email", "Phone"]


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_delimiters(delimiter):
    data = make_csv(HEADERS, ROWS, delimiter=delimiter)
    parsed = fp.parse_csv(data)
    assert parsed.columns == HEADERS
    assert parsed.delimiter == delimiter
    assert parsed.row_count == 2
    assert parsed.rows[0][2] == "052-555-1234"
    assert parsed.rows[1][2] is None  # empty -> None


def test_csv_utf8_bom():
    data = make_csv(HEADERS, ROWS, encoding="utf-8-sig")
    parsed = fp.parse_csv(data)
    assert parsed.encoding == "UTF-8-SIG"
    assert parsed.columns[0] == "Name"  # BOM stripped from the first header


def test_csv_legacy_encoding_fallback():
    data = "Name;City\nJosé;São Paulo\n".encode("cp1252")
    parsed = fp.parse_csv(data)
    assert parsed.rows[0][0] in ("José", "JosÃ©")  # decoded, never crashes
    assert parsed.column_count == 2


def test_csv_hebrew_and_arabic_utf8():
    data = make_csv(["שם", "الدولة"], [["דנה", "الإمارات"]])
    parsed = fp.parse_csv(data)
    assert parsed.rows[0] == ["דנה", "الإمارات"]


def test_csv_duplicate_headers_are_disambiguated():
    data = make_csv(["Email", "Email", ""], [["a@x.com", "b@x.com", "z"]])
    parsed = fp.parse_csv(data)
    assert parsed.columns == ["Email", "Email (2)", "Column 3"]
    profile = fp.inspect(parsed)
    assert profile["Email"]["is_duplicate_header"] is True


def test_csv_ragged_rows_are_padded_and_trimmed():
    raw = b"a,b,c\n1,2\n1,2,3,4\n\n\n"
    parsed = fp.parse_csv(raw)
    assert parsed.rows == [["1", "2", None], ["1", "2", "3"]]


def test_empty_file_rejected():
    with pytest.raises(InvalidFileError):
        fp.parse_csv(b"")
    with pytest.raises(InvalidFileError):
        fp.parse_csv(b"   \n\n")


def test_invalid_xlsx_rejected():
    with pytest.raises(InvalidFileError):
        fp.parse_xlsx(b"this is not a zip file")
    with pytest.raises(InvalidFileError):
        fp.parse_upload("data.xlsx", b"PK\x03\x04garbage")


def test_unknown_extension_rejected():
    with pytest.raises(InvalidFileError):
        fp.parse_upload("data.txt", b"a,b\n1,2")


def test_xlsx_basic_and_types():
    from datetime import date, datetime

    data = make_xlsx(
        ["Name", "Joined", "Amount", "Active", "When"],
        [["Dana", date(2026, 9, 21), 12.5, True, datetime(2026, 9, 21, 13, 45)]],
    )
    parsed = fp.parse_xlsx(data)
    assert parsed.file_type == "xlsx"
    assert parsed.rows[0] == ["Dana", "2026-09-21", "12.5", "true", "2026-09-21 13:45:00"]


def test_xlsx_multiple_sheets():
    data = make_xlsx(HEADERS, ROWS, sheets={"Second": [["x", "y"], ["1", "2"]]})
    assert fp.list_sheets(data) == ["Sheet1", "Second"]
    first = fp.parse_xlsx(data)
    assert first.sheet_name == "Sheet1" and first.sheet_names == ["Sheet1", "Second"]
    second = fp.parse_xlsx(data, "Second")
    assert second.columns == ["x", "y"] and second.rows == [["1", "2"]]
    with pytest.raises(InvalidFileError):
        fp.parse_xlsx(data, "Nope")


def test_xlsx_formulas_are_never_evaluated():
    """A formula cell has no cached value in a freshly written workbook -> None."""
    data = make_xlsx(["Name", "Calc"], [["Dana", '=HYPERLINK("http://evil","x")']])
    parsed = fp.parse_xlsx(data)
    assert parsed.rows[0][1] is None


def test_inspect_profile():
    data = make_csv(
        ["Email", "Phone", "Empty", "Country", "Joined", "Flag", "N"],
        [
            ["a@x.com", "+972525551234", "", "IL", "2026-01-02", "yes", "1"],
            ["b@x.com", "052-555-1234", "", "US", "03/04/2025", "no", "2"],
            ["c@x.com", "+44 7911 123456", "", "IL", "Sep 21 2026", "yes", "3"],
        ],
    )
    profile = fp.inspect(fp.parse_csv(data))
    assert profile["Email"]["detected_type"] == "email"
    assert profile["Phone"]["detected_type"] == "phone"
    assert profile["Empty"]["is_empty"] and profile["Empty"]["empty_pct"] == 100.0
    assert profile["Country"]["detected_type"] == "country_code"
    assert profile["Country"]["unique_pct"] == pytest.approx(66.7, abs=0.1)
    assert profile["Joined"]["detected_type"] == "date"
    assert profile["Flag"]["detected_type"] == "boolean"
    assert profile["N"]["detected_type"] == "integer"
    assert profile["Email"]["examples"] == ["a@x.com", "b@x.com", "c@x.com"]
