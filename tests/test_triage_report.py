"""The downloadable CSV report: header, plain amounts, source rows, quoting."""
import csv
import io

import pytest
from triage.engine import TriageException
from triage.report import HEADER, to_csv


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


def test_header_is_the_planned_one():
    assert HEADER == ["id", "type", "severity", "invoice_no", "vendor", "po_no", "payment_ids", "amount_at_risk",
                      "suggested_action", "explanation", "source_rows"]
    first_line = to_csv([], {}).splitlines()[0]
    assert first_line == "id,type,severity,invoice_no,vendor,po_no,payment_ids,amount_at_risk,suggested_action," \
                         "explanation,source_rows"


def test_empty_report_is_just_the_header():
    assert len(to_csv([], {}).splitlines()) == 1


def test_one_row_has_every_column_filled():
    row = parse(to_csv([make_exception()], {"EX-01": "Invoice INV/42 repeats INV-0042."}))[0]
    assert row["id"] == "EX-01"
    assert row["type"] == "duplicate_invoice"
    assert row["severity"] == "high"
    assert row["invoice_no"] == "INV/42"
    assert row["vendor"] == "Sundaram Brake Linings Pvt Ltd"
    assert row["po_no"] == "PO-00101"
    assert row["payment_ids"] == "PV/1;PV/2"
    assert row["suggested_action"].startswith("Hold payment")
    assert row["explanation"] == "Invoice INV/42 repeats INV-0042."


def test_amount_is_plain_two_decimals_with_no_symbol_or_grouping():
    rows = parse(to_csv([make_exception(amount_at_risk=145140.0),
                         make_exception(id="EX-02", amount_at_risk=0.0),
                         make_exception(id="EX-03", amount_at_risk=1234567.5)], {}))
    assert [r["amount_at_risk"] for r in rows] == ["145140.00", "0.00", "1234567.50"]


def test_source_rows_lists_each_file_with_its_rows():
    row = parse(to_csv([make_exception()], {}))[0]
    assert row["source_rows"] == "invoices:17,5; purchase_orders:9; payments:3,4"


def test_source_rows_leaves_out_files_with_no_rows():
    exc = make_exception(source_rows={"invoices": [22], "purchase_orders": [], "payments": []},
                         po_ids=[], payment_ids=[], facts={"invoice_no": "AS/0071", "vendor": "Apex", "po_no": ""})
    row = parse(to_csv([exc], {}))[0]
    assert row["source_rows"] == "invoices:22"
    assert row["po_no"] == ""
    assert row["payment_ids"] == ""


def test_missing_explanation_is_an_empty_cell():
    row = parse(to_csv([make_exception()], {}))[0]
    assert row["explanation"] == ""


def test_commas_quotes_and_newlines_survive_a_round_trip():
    nasty = 'Vendor "A, B" & Sons\nSecond line'
    exc = make_exception(facts={"invoice_no": "INV/42", "vendor": nasty, "po_no": ""})
    row = parse(to_csv([exc], {"EX-01": 'He said "hold", then left.'}))[0]
    assert row["vendor"] == nasty
    assert row["explanation"] == 'He said "hold", then left.'


@pytest.mark.parametrize("cell", ["=HYPERLINK(\"http://x\")", "+91 98765", "@SUM(A1)", "-2+3"])
def test_cells_that_start_like_a_formula_are_defused(cell):
    exc = make_exception(facts={"invoice_no": "INV/42", "vendor": cell, "po_no": ""})
    row = parse(to_csv([exc], {}))[0]
    assert row["vendor"] == "'" + cell


def test_rupee_sign_and_hinglish_pass_through_unchanged():
    row = parse(to_csv([make_exception()], {"EX-01": "Yeh bill ₹1,45,140 ka duplicate hai."}))[0]
    assert row["explanation"] == "Yeh bill ₹1,45,140 ka duplicate hai."


def test_report_has_no_byte_order_mark_because_the_server_adds_it():
    assert not to_csv([make_exception()], {}).startswith("﻿")
