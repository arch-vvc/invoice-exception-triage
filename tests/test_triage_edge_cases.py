"""Bad and awkward uploads: each one must raise InputError with a message a finance user can act on.

Every case runs through load_table and through triage_bytes (with the other two sample files intact).
The HTTP side of this (a 400 JSON body) is tested with the server in test_triage_server.py.
"""
import csv
import io
import re
from pathlib import Path

import pytest
from triage.engine import SAMPLE_FILES, triage_bytes
from triage.loader import InputError, load_table

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
SAMPLE = {kind: (name, (SAMPLE_DIR / name).read_bytes()) for kind, name in SAMPLE_FILES.items()}
INVOICE_TEXT = SAMPLE["invoices"][1].decode("utf-8")
INVOICE_HEADER = ("Date,Bill Number,Party Name,GSTIN/UIN,Order No,Particulars,Billed Qty,Rate,Taxable Value,GST %,"
                  "Tax Amount,Invoice Amount")
FORBIDDEN = ["Traceback", "Error", "Exception", "NoneType", "KeyError", "codec", "0x"]


def via_load_table(kind, data):
    return load_table(data, kind, SAMPLE[kind][0])


def via_triage_bytes(kind, data):
    files = dict(SAMPLE)
    files[kind] = (SAMPLE[kind][0], data)
    return triage_bytes(files)


ENTRY_POINTS = [pytest.param(via_load_table, id="load_table"), pytest.param(via_triage_bytes, id="triage_bytes")]


def sentences(message: str) -> int:
    """Sentences in the message, not counting the 'We looked for: ... Found: ...' lists of a missing column."""
    prose = re.sub(r"\s*We looked for:.*?Found:.*?\.\s*", " ", message)
    return len(re.findall(r"[.!?](?:\s|$)", prose))


def assert_friendly(error: InputError, filename: str):
    message = str(error)
    assert filename in message
    for word in FORBIDDEN:
        assert word not in message, f"{word!r} leaked into: {message}"
    assert 1 <= sentences(message) <= 2, f"contract C4 allows at most 2 sentences: {message}"


def rewrite_csv(text: str, edit_row, delimiter: str = ",") -> bytes:
    """Parse a CSV, apply edit_row(cells) -> cells to every row, and write it back as UTF-8."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    for cells in csv.reader(io.StringIO(text, newline="")):
        writer.writerow(edit_row(cells))
    return out.getvalue().encode("utf-8")


# ---- inputs that must be rejected ----

@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("data", [
    b"",
    b"   \n\t\r\n  ",
    b"\xef\xbb\xbf",  # a BOM and nothing else
    "﻿".encode("utf-16"),
], ids=["empty", "whitespace", "bom_only", "utf16_bom_only"])
def test_empty_input(entry, data):
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")
    assert "empty" in str(caught.value)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("kind", list(SAMPLE_FILES))
def test_header_with_no_data_rows(entry, kind):
    lines = SAMPLE[kind][1].decode("utf-8").splitlines()
    header_at = next(i for i, line in enumerate(lines) if line.startswith(("Date,", "PO#", "Txn Date")))
    data = ("\n".join(lines[:header_at + 1]) + "\n").encode("utf-8")
    with pytest.raises(InputError) as caught:
        entry(kind, data)
    assert_friendly(caught.value, SAMPLE[kind][0])
    assert "no data rows" in str(caught.value)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_header_and_footer_only_is_still_no_data(entry):
    data = f"{INVOICE_HEADER}\n,,,,,,,,,,,\"Grand Total\"\nGrand Total,,,,,,,,,,,\"1,00,000.00\"\n".encode()
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_missing_required_column_lists_what_we_looked_for(entry):
    def drop_order_no(cells):
        return cells[:4] + cells[5:] if len(cells) > 5 else cells

    data = rewrite_csv(INVOICE_TEXT, drop_order_no)
    assert b"Order No" not in data
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    message = str(caught.value)
    assert_friendly(caught.value, "invoices.csv")
    assert "PO reference" in message
    assert "We looked for" in message
    for alias in ("Order No", "PO No", "PO Number"):
        assert alias in message
    assert "Found:" in message or "found:" in message
    assert "Party Name" in message  # shows the columns it did find


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_unknown_amount_column_name(entry):
    data = INVOICE_TEXT.replace("Invoice Amount", "Mystery Figure", 1).encode("utf-8")
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    message = str(caught.value)
    assert_friendly(caught.value, "invoices.csv")
    assert "invoice total" in message
    assert "Invoice Amount" in message  # the aliases tried
    assert "Mystery Figure" in message  # what the file had instead


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_payments_missing_invoice_reference(entry):
    text = SAMPLE["payments"][1].decode("utf-8").replace("Against Bill", "Remarks", 1)
    with pytest.raises(InputError) as caught:
        entry("payments", text.encode("utf-8"))
    assert_friendly(caught.value, "payments.csv")
    assert "invoice reference" in str(caught.value)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_no_recognisable_header_at_all(entry):
    data = b"hello,world\n1,2\n3,4\n"
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("encoding, vendor", [
    ("cp1252", "Sundaram – Brake Linings Pvt Ltd"),
    ("cp1252", "Café Packaging Pvt Ltd"),
    ("latin-1", "Café Packaging Pvt Ltd"),
])
def test_wrong_encoding(entry, encoding, vendor):
    text = INVOICE_TEXT.replace("Sundaram Brake Linings Pvt Ltd", vendor, 1)
    assert text != INVOICE_TEXT
    data = text.replace("\u20b9", "Rs ").encode(encoding)  # the sample's rupee sign has no cp1252 or latin-1 byte
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")
    assert "UTF-8" in str(caught.value)
    assert "Save As" in str(caught.value)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("data", [
    b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00" + b"\x00\x13\x8a" * 50,
    b"PK\x03\x04",
], ids=["xlsx_like", "zip_magic_only"])
def test_xlsx_bytes(entry, data):
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")
    assert "Excel" in str(caught.value)
    assert "CSV" in str(caught.value)


def garbage(length: int, with_nulls: bool) -> bytes:
    """Deterministic junk from a small linear congruential generator, so the test never flakes."""
    value, out = 12345, bytearray()
    for _ in range(length):
        value = (value * 1103515245 + 12345) % 2**31
        byte = (value >> 16) & 0xFF
        out.append(byte if with_nulls or byte else 1)
    return bytes(out)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("data", [
    garbage(2000, with_nulls=True),
    garbage(2000, with_nulls=False),
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + garbage(200, with_nulls=True),
    b"\xff\xfe\x00",  # a UTF-16 BOM followed by half a character
    b"\x80\x81\x82\x83",
], ids=["noise_with_nulls", "noise_no_nulls", "png_header", "truncated_utf16", "bare_continuation_bytes"])
def test_garbage_binary(entry, data):
    with pytest.raises(InputError) as caught:
        entry("invoices", data)
    assert_friendly(caught.value, "invoices.csv")


@pytest.mark.parametrize("kind", list(SAMPLE_FILES))
def test_error_names_the_uploaded_filename_not_the_default(kind):
    with pytest.raises(InputError) as caught:
        load_table(b"", kind, "Q3 vendor bills (final).csv")
    assert "Q3 vendor bills (final).csv" in str(caught.value)


@pytest.mark.parametrize("kind", list(SAMPLE_FILES))
def test_triage_bytes_with_a_missing_kind_names_it(kind):
    files = {k: v for k, v in SAMPLE.items() if k != kind}
    with pytest.raises(InputError) as caught:
        triage_bytes(files)
    assert_friendly(caught.value, SAMPLE_FILES[kind])


def test_triage_bytes_with_no_files_at_all():
    with pytest.raises(InputError) as caught:
        triage_bytes({})
    assert "invoices.csv" in str(caught.value)


def test_an_error_in_one_file_names_that_file_only():
    files = dict(SAMPLE)
    files["payments"] = ("payments.csv", b"")
    with pytest.raises(InputError) as caught:
        triage_bytes(files)
    assert "payments.csv" in str(caught.value)
    assert "invoices.csv" not in str(caught.value)


# ---- formats that must be accepted, and must give the same answer as the sample ----

@pytest.fixture(scope="module")
def baseline():
    return triage_bytes(SAMPLE)


def same_result(result, baseline):
    assert result.exceptions == baseline.exceptions
    assert result.warnings == baseline.warnings
    assert result.unmatched_payment_ids == baseline.unmatched_payment_ids
    assert result.invoices_checked == baseline.invoices_checked == 40


def convert_all(convert):
    return {kind: (name, convert(data)) for kind, (name, data) in SAMPLE.items()}


def test_utf16_with_bom_is_accepted(baseline):
    files = convert_all(lambda data: data.decode("utf-8").encode("utf-16"))
    assert all(data[1][:2] in (b"\xff\xfe", b"\xfe\xff") for data in files.values())
    same_result(triage_bytes(files), baseline)


def test_utf16_big_endian_with_bom_is_accepted(baseline):
    files = convert_all(lambda data: b"\xfe\xff" + data.decode("utf-8").encode("utf-16-be"))
    same_result(triage_bytes(files), baseline)


def test_utf8_with_bom_is_accepted(baseline):
    files = convert_all(lambda data: b"\xef\xbb\xbf" + data)
    same_result(triage_bytes(files), baseline)


def test_a_doubled_bom_is_accepted(baseline):
    files = convert_all(lambda data: b"\xef\xbb\xbf\xef\xbb\xbf" + data)
    same_result(triage_bytes(files), baseline)


def test_crlf_line_endings_are_accepted(baseline):
    files = convert_all(lambda data: data.replace(b"\n", b"\r\n"))
    assert b"\r\n" in files["invoices"][1]
    same_result(triage_bytes(files), baseline)


def test_old_mac_cr_only_line_endings_are_accepted(baseline):
    files = convert_all(lambda data: data.replace(b"\n", b"\r"))
    same_result(triage_bytes(files), baseline)


@pytest.mark.parametrize("delimiter", [";", "\t"], ids=["semicolon", "tab"])
def test_other_delimiters_are_accepted_for_every_file(baseline, delimiter):
    files = convert_all(lambda data: rewrite_csv(data.decode("utf-8"), lambda cells: cells, delimiter))
    assert delimiter.encode() in files["payments"][1]
    same_result(triage_bytes(files), baseline)


def test_semicolon_payments_alone_match_the_comma_version():
    comma = load_table(SAMPLE["payments"][1], "payments", "payments.csv")
    semicolon_bytes = rewrite_csv(SAMPLE["payments"][1].decode("utf-8"), lambda cells: cells, ";")
    semicolon = load_table(semicolon_bytes, "payments", "payments.csv")
    assert semicolon.header == comma.header
    assert semicolon.rows == comma.rows


def test_trailing_blank_lines_and_spaces_around_cells_are_accepted(baseline):
    files = convert_all(lambda data: data + b"\n\n   \n,,,,\n")
    same_result(triage_bytes(files), baseline)
