"""The downloadable CSV report: header, plain amounts, source rows, quoting."""
import csv
import io

import pytest

from triage.engine import TriageException
from triage.report import HEADER, to_csv, to_csv_bytes


def make_exception(**overrides) -> TriageException:
    values = {
        "id": "EX-01", "type": "duplicate_invoice", "severity": "high",
        "invoice_ids": ["INV/42", "INV-0042"], "po_ids": ["PO-00101"], "payment_ids": ["PV/1", "PV/2"],
        "source_rows": {"invoices": [17, 5], "purchase_orders": [9], "payments": [3, 4]},
        "amount_at_risk": 145140.0, "suggested_action": "Hold payment on the duplicate and confirm with vendor",
        "facts": {"invoice_no": "INV/42", "vendor": "Sundaram Brake Linings Pvt Ltd", "po_no": "PO-00101"},
    }
    values.update(overrides)
    return TriageException(**values)


def parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text, newline="")))


PLAIN_HEADER = ["Exception", "Severity", "Invoice", "Vendor", "PO", "Amount at risk (₹)", "What went wrong",
                "Suggested action", "Source rows"]


def test_header_row_is_plain_words_in_order():
    assert HEADER == PLAIN_HEADER
    assert to_csv([], {}).splitlines()[0] == ",".join(PLAIN_HEADER)


def test_empty_report_is_just_the_header():
    assert len(to_csv([], {}).splitlines()) == 1


def test_duplicate_invoice_row_has_plain_values():
    row = parse(to_csv([make_exception()], {"EX-01": "Invoice INV/42 repeats INV-0042."}))[0]
    assert row["Exception"] == "Duplicate invoice"
    assert row["Severity"] == "High"
    assert row["Invoice"] == "INV/42"
    assert row["Vendor"] == "Sundaram Brake Linings Pvt Ltd"
    assert row["PO"] == "PO-00101"
    assert row["What went wrong"] == "Invoice INV/42 repeats INV-0042."
    assert row["Suggested action"].startswith("Hold payment")


def test_blank_po_stays_blank_not_none():
    exc = make_exception(source_rows={"invoices": [22], "purchase_orders": [], "payments": []},
                         po_ids=[], payment_ids=[], facts={"invoice_no": "AS/0071", "vendor": "Apex", "po_no": None})
    text = to_csv([exc], {})
    assert "None" not in text
    assert parse(text)[0]["PO"] == ""


def test_amount_parses_as_a_plain_number():
    rows = parse(to_csv([make_exception(amount_at_risk=145140.0),
                         make_exception(id="EX-02", amount_at_risk=0.0),
                         make_exception(id="EX-03", amount_at_risk=1234567.5)], {}))
    values = [r["Amount at risk (₹)"] for r in rows]
    assert [float(v) for v in values] == [145140.0, 0.0, 1234567.5]
    assert all(v.replace(".", "").isdigit() for v in values)  # no symbol, no commas


def test_source_rows_from_two_files_read_in_plain_words():
    row = parse(to_csv([make_exception(source_rows={"invoices": [17, 6], "purchase_orders": [],
                                                    "payments": [4]})], {}))[0]
    assert row["Source rows"] == "Invoices row 17, 6; Payments row 4"


def test_source_rows_name_every_file_that_has_rows():
    row = parse(to_csv([make_exception()], {}))[0]
    assert row["Source rows"] == "Invoices row 17, 5; Purchase orders row 9; Payments row 3, 4"


def test_source_rows_leaves_out_files_with_no_rows():
    exc = make_exception(source_rows={"invoices": [22], "purchase_orders": [], "payments": []})
    assert parse(to_csv([exc], {}))[0]["Source rows"] == "Invoices row 22"


def test_missing_explanation_is_an_empty_cell():
    row = parse(to_csv([make_exception()], {}))[0]
    assert row["What went wrong"] == ""


def test_commas_quotes_and_newlines_survive_a_round_trip():
    nasty = 'Vendor "A, B" & Sons\nSecond line'
    exc = make_exception(facts={"invoice_no": "INV/42", "vendor": nasty, "po_no": ""})
    row = parse(to_csv([exc], {"EX-01": 'He said "hold", then left.'}))[0]
    assert row["Vendor"] == nasty
    assert row["What went wrong"] == 'He said "hold", then left.'


@pytest.mark.parametrize("cell", ["=HYPERLINK(\"http://x\")", "+91 98765", "@SUM(A1)", "-2+3"])
def test_cells_that_start_like_a_formula_are_defused(cell):
    exc = make_exception(facts={"invoice_no": "INV/42", "vendor": cell, "po_no": ""})
    row = parse(to_csv([exc], {}))[0]
    assert row["Vendor"] == "'" + cell


def test_rupee_sign_and_hinglish_pass_through_unchanged():
    row = parse(to_csv([make_exception()], {"EX-01": "Yeh bill ₹1,45,140 ka duplicate hai."}))[0]
    assert row["What went wrong"] == "Yeh bill ₹1,45,140 ka duplicate hai."


def test_text_report_has_no_byte_order_mark_but_the_bytes_start_with_one():
    assert not to_csv([make_exception()], {}).startswith("\ufeff")
    data = to_csv_bytes([make_exception()], {})
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8-sig").splitlines()[0].startswith("Exception,Severity")
    assert "₹".encode() in data
