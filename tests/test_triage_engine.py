"""Engine tests: linking (C3) and the exception catalogue (C5) on tiny inline CSVs.

The CSVs use header aliases that are not the ones in the sample data, on purpose.
Row numbers: line 1 is the header, so the first data row is line 2.
"""
import json
import socket
import subprocess

import pytest

from triage.engine import Tolerance, TriageException, triage, triage_bytes, triage_dir
from triage.loader import InputError, load_table
from triage.normalise import to_invoices, to_payments, to_purchase_orders

GSTIN = "27ABCDE1234F1Z0"
BAD_CHECKSUM_GSTIN = "27ABCDE1234F1Z1"
BAD_FORMAT_GSTIN = "27ABCDE1234F1"

INVOICE_HEADER = "Inv No,Supplier,GSTIN,Invoice Date,PO Ref,Qty,Unit Price,Taxable Amt,GST Rate,GST Amount,Total"
PO_HEADER = "Order Number,Supplier,Qty Ordered,Price,GST %,GSTIN"
PAYMENT_HEADER = "Payment No,Beneficiary,Bill Ref,Amount,TDS"


def inv(no, vendor="Acme Traders", gstin=GSTIN, date="01/05/2024", po="PO-1", qty=10, rate=100, gst=18,
        taxable=None, gst_amount=None, total=None):
    """One invoice CSV line. Taxable, GST amount and total default to consistent values."""
    taxable = qty * rate if taxable is None else taxable
    gst_amount = round(taxable * gst / 100, 2) if gst_amount is None else gst_amount
    total = round(taxable + gst_amount, 2) if total is None else total
    return f'{no},"{vendor}",{gstin},{date},{po},{qty},{rate},{taxable},{gst},{gst_amount},{total}'


def po(no="PO-1", vendor="Acme Traders", qty=10, rate=100, gst=18):
    return f'{no},"{vendor}",{qty},{rate},{gst},{GSTIN}'


def pay(payment_id, ref, paid, tds=0, payee="Acme Traders"):
    return f'{payment_id},"{payee}",{ref},{paid},{tds}'


def run(invoices, pos=(), payments=(), tol=None, expect_warnings=False):
    """Load the three CSVs from lines and run the engine. Empty lists skip the file."""
    warnings: list[str] = []

    def table(kind, header, lines):
        return load_table(("\n".join([header, *lines]) + "\n").encode(), kind, f"{kind}.csv") if lines else None

    t_inv, t_po, t_pay = (
        table("invoices", INVOICE_HEADER, invoices),
        table("purchase_orders", PO_HEADER, pos),
        table("payments", PAYMENT_HEADER, payments),
    )
    result = triage(
        to_invoices(t_inv, warnings) if t_inv else [],
        to_purchase_orders(t_po, warnings) if t_po else [],
        to_payments(t_pay, warnings) if t_pay else [],
        tol,
    )
    assert expect_warnings or warnings == []
    return result


def only(result) -> TriageException:
    assert len(result.exceptions) == 1, [(e.type, e.invoice_ids) for e in result.exceptions]
    return result.exceptions[0]


def types(result) -> list[str]:
    return [e.type for e in result.exceptions]


# ---- baseline ----

def test_clean_three_way_match_has_no_exceptions():
    result = run([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180)])
    assert result.exceptions == []
    assert result.invoices_checked == 1
    assert result.amount_at_risk == 0
    assert result.unmatched_payment_ids == [] and result.warnings == []


def test_unpaid_invoice_is_not_an_exception():
    assert run([inv("INV-1")], [po()]).exceptions == []


# ---- duplicate_invoice ----

def test_duplicate_invoice_flags_the_later_row():
    result = run([inv("INV-0042"), inv("inv/42", date="05/05/2024")], [po()])
    exc = only(result)
    assert exc.type == "duplicate_invoice" and exc.severity == "high"
    assert exc.invoice_ids == ["inv/42", "INV-0042"]
    assert exc.source_rows["invoices"] == [3, 2]
    assert exc.amount_at_risk == 1180.0
    assert exc.facts["original_invoice_no"] == "INV-0042"
    assert exc.facts["total"] == 1180.0 and exc.facts["total_display"] == "₹1,180"
    assert exc.suggested_action == "Hold payment on the duplicate and confirm with vendor"


def test_same_number_with_a_different_total_is_not_a_duplicate():
    result = run([inv("INV-42"), inv("INV/42", qty=20)], [po(qty=30)])
    assert result.exceptions == []


def test_same_number_from_another_vendor_is_not_a_duplicate():
    result = run([inv("INV-42"), inv("INV-42", vendor="Other Co", po="PO-2")], [po(), po("PO-2", "Other Co")])
    assert result.exceptions == []


def test_duplicate_is_left_out_of_quantity_and_payment_checks():
    result = run([inv("INV-1"), inv("INV-01")], [po()], [pay("P1", "INV-1", 1180)])
    assert types(result) == ["duplicate_invoice"]


# ---- missing_po and the C3 fallbacks ----

def test_blank_po_ref_with_one_po_for_the_vendor_links_by_vendor():
    result = run([inv("INV-1", po="")], [po()])
    assert result.exceptions == []


def test_blank_po_ref_with_two_pos_for_the_vendor_is_missing_po():
    result = run([inv("INV-1", po="")], [po("PO-1"), po("PO-2")])
    exc = only(result)
    assert exc.type == "missing_po" and exc.severity == "medium"
    assert exc.amount_at_risk == 1180.0
    assert exc.facts["po_ref_given"] == "" and exc.facts["po_no"] == ""
    assert exc.po_ids == []


def test_blank_po_ref_with_no_po_for_the_vendor_is_missing_po():
    exc = only(run([inv("INV-1", po="")], [po(vendor="Someone Else")]))
    assert exc.type == "missing_po"


def test_unknown_po_ref_is_missing_po_even_when_the_vendor_has_one_po():
    exc = only(run([inv("INV-1", po="PO-404")], [po()]))
    assert exc.type == "missing_po"
    assert exc.facts["po_ref_given"] == "PO-404"


def test_missing_po_is_high_from_one_lakh():
    exc = only(run([inv("INV-1", po="PO-404", qty=1000)]))
    assert exc.amount_at_risk == 118000.0 and exc.severity == "high"
    assert exc.source_rows == {"invoices": [2], "purchase_orders": [], "payments": []}


def test_po_ref_matches_regardless_of_spelling():
    assert run([inv("INV-1", po="po 001")], [po("PO/0001"), po("PO-2")]).exceptions == []


# ---- vendor_name_mismatch ----

def test_vendor_name_mismatch_is_low_and_carries_no_money():
    result = run([inv("INV-1", vendor="Sri Ganesh Steel")], [po(vendor="Shree Ganesh Steels")])
    exc = only(result)
    assert exc.type == "vendor_name_mismatch" and exc.severity == "low"
    assert exc.amount_at_risk == 0.0
    assert exc.facts["invoice_vendor"] == "Sri Ganesh Steel"
    assert exc.facts["po_vendor"] == "Shree Ganesh Steels"
    assert exc.po_ids == ["PO-1"] and exc.source_rows["purchase_orders"] == [2]


def test_ms_prefix_and_company_suffix_are_not_a_vendor_mismatch():
    result = run(
        [inv("INV-1", vendor="KUMAR PACKAGING PRIVATE LIMITED")],
        [po(vendor="M/s. Kumar Packaging Pvt. Ltd.")],
    )
    assert result.exceptions == []


# ---- amount_mismatch ----

def test_amount_mismatch_when_invoice_rate_exceeds_po_rate():
    exc = only(run([inv("INV-1", rate=110)], [po()]))
    assert exc.type == "amount_mismatch" and exc.severity == "medium"
    assert exc.amount_at_risk == pytest.approx(100 * 1.18)
    assert exc.facts["invoice_rate"] == 110 and exc.facts["po_rate"] == 100
    assert exc.facts["qty"] == 10 and exc.facts["diff"] == 100
    assert exc.facts["diff_display"] == "₹100"
    assert exc.suggested_action == "Ask vendor for a credit note for the price difference"


def test_amount_mismatch_is_high_from_ten_thousand():
    exc = only(run([inv("INV-1", qty=1000, rate=110)], [po(qty=1000)]))
    assert exc.severity == "high" and exc.amount_at_risk == pytest.approx(11800)


def test_small_rate_difference_inside_one_percent_is_tolerated():
    assert run([inv("INV-1", rate=100.5)], [po()]).exceptions == []


def test_tolerance_is_configurable():
    result = run([inv("INV-1", rate=100.5)], [po()], tol=Tolerance(amount_pct=0.0, amount_abs=0.0))
    assert types(result) == ["amount_mismatch"]


# ---- quantity_mismatch ----

def test_quantity_mismatch_when_second_invoice_overshoots_the_po():
    result = run([inv("INV-1", qty=60), inv("INV-2", qty=60, date="10/05/2024")], [po(qty=100)])
    exc = only(result)
    assert exc.type == "quantity_mismatch" and exc.severity == "medium"
    assert exc.invoice_ids == ["INV-2", "INV-1"]
    assert exc.amount_at_risk == pytest.approx(20 * 100 * 1.18)
    assert (exc.facts["invoiced_qty"], exc.facts["po_qty"], exc.facts["excess_qty"]) == (120, 100, 20)
    assert exc.source_rows["invoices"][0] == 3


def test_quantity_is_cumulated_in_date_order_not_file_order():
    result = run([inv("INV-2", qty=60, date="10/05/2024"), inv("INV-1", qty=60)], [po(qty=100)])
    assert only(result).invoice_ids[0] == "INV-2"


def test_split_po_within_quantity_is_fine():
    result = run([inv("INV-1", qty=60), inv("INV-2", qty=40, date="10/05/2024")], [po(qty=100)])
    assert result.exceptions == []


def test_under_billing_is_allowed():
    assert run([inv("INV-1", qty=5)], [po(qty=10)]).exceptions == []


def test_every_invoice_after_the_po_is_used_up_is_fully_excess():
    invoices = [inv("INV-1", qty=100), inv("INV-2", qty=10, date="02/05/2024"), inv("INV-3", qty=10, date="03/05/2024")]
    result = run(invoices, [po(qty=100)])
    assert sorted(e.invoice_ids[0] for e in result.exceptions) == ["INV-2", "INV-3"]
    assert {e.facts["excess_qty"] for e in result.exceptions} == {10}


def test_quantity_mismatch_is_high_from_ten_thousand():
    result = run([inv("INV-1", qty=700), inv("INV-2", qty=700, date="10/05/2024")], [po(qty=1000)])
    assert only(result).severity == "high"


# ---- gst_error ----

def gst_exc(line, **po_args):
    exc = only(run([line], [po(**po_args)]))
    assert exc.type == "gst_error"
    return exc


def test_gst_error_missing_gstin():
    exc = gst_exc(inv("INV-1", gstin=""))
    assert exc.facts["reason"] == "missing" and exc.severity == "high"
    assert exc.amount_at_risk == 180.0
    assert exc.suggested_action == "Get a corrected tax invoice before claiming ITC"


def test_missing_gstin_is_fine_when_gst_is_zero():
    assert run([inv("INV-1", gstin="", gst=0)], [po(gst=0)]).exceptions == []


def test_gst_error_bad_format():
    exc = gst_exc(inv("INV-1", gstin=BAD_FORMAT_GSTIN))
    assert exc.facts["reason"] == "format" and exc.severity == "high"
    assert exc.facts["gstin"] == BAD_FORMAT_GSTIN


def test_gst_error_bad_checksum():
    exc = gst_exc(inv("INV-1", gstin=BAD_CHECKSUM_GSTIN))
    assert exc.facts["reason"] == "checksum" and exc.severity == "high"


def test_gst_error_rate_not_a_slab():
    exc = gst_exc(inv("INV-1", gst=15))
    assert exc.facts["reason"] == "slab" and exc.severity == "medium"
    assert exc.amount_at_risk == 150.0


def test_gst_error_rate_differs_from_po():
    exc = gst_exc(inv("INV-1", gst=12))
    assert exc.facts["reason"] == "rate_vs_po" and exc.severity == "medium"
    assert exc.amount_at_risk == pytest.approx(60.0)
    assert exc.facts["gst_rate"] == 12 and exc.facts["po_gst_rate"] == 18


def test_gst_error_amount_does_not_match_rate():
    exc = gst_exc(inv("INV-1", gst_amount=150, total=1150))
    assert exc.facts["reason"] == "amount" and exc.severity == "medium"
    assert exc.amount_at_risk == 30.0


def test_gst_amount_rounded_off_by_fifty_paise_is_fine():
    assert run([inv("INV-1", gst_amount=180.5, total=1180.5)], [po()]).exceptions == []


def test_gst_error_reports_only_the_first_reason():
    exc = gst_exc(inv("INV-1", gstin=BAD_CHECKSUM_GSTIN, gst=12))
    assert exc.facts["reason"] == "checksum"


def test_gst_checks_that_need_no_po_still_run_on_unlinked_invoices():
    result = run([inv("INV-1", po="PO-404", gstin="")], [po()])
    assert sorted(types(result)) == ["gst_error", "missing_po"]


def test_blank_gst_amount_skips_the_amount_check():
    line = inv("INV-1").replace(",180.0,1180.0", ",,1180.0")
    assert run([line], [po()]).exceptions == []


# ---- partial_payment and overpayment ----

def big(no="INV-1"):
    return inv(no, qty=1000), po(qty=1000)  # total 1,18,000


def test_partial_payment_low_under_fifty_thousand_outstanding():
    invoice, order = big()
    exc = only(run([invoice], [order], [pay("P1", "INV-1", 100000)]))
    assert exc.type == "partial_payment" and exc.severity == "low"
    assert exc.amount_at_risk == 18000.0
    assert exc.payment_ids == ["P1"] and exc.source_rows["payments"] == [2]
    assert exc.facts["total"] == 118000.0 and exc.facts["paid"] == 100000.0
    assert exc.facts["tds"] == 0.0 and exc.facts["outstanding"] == 18000.0
    assert exc.facts["outstanding_display"] == "₹18,000"
    assert exc.suggested_action == "Confirm the reason for short payment or schedule the balance"


def test_partial_payment_medium_from_fifty_thousand_outstanding():
    invoice, order = big()
    exc = only(run([invoice], [order], [pay("P1", "INV-1", 60000)]))
    assert exc.severity == "medium" and exc.amount_at_risk == 58000.0


def test_payment_short_by_less_than_a_rupee_is_fine():
    invoice, order = big()
    assert run([invoice], [order], [pay("P1", "INV-1", 117999.5)]).exceptions == []


def test_overpayment_is_high():
    invoice, order = big()
    exc = only(run([invoice], [order], [pay("P1", "INV-1", 120000)]))
    assert exc.type == "overpayment" and exc.severity == "high"
    assert exc.amount_at_risk == 2000.0
    assert exc.facts["excess"] == 2000.0 and exc.facts["excess_display"] == "₹2,000"
    assert exc.suggested_action == "Raise a refund request or adjust against the next bill"


def test_payment_with_tds_that_settles_in_full_is_fine():
    invoice, order = big()
    assert run([invoice], [order], [pay("P1", "INV-1", 116000, tds=2000)]).exceptions == []


def test_tds_counts_towards_settlement_in_a_partial_payment_too():
    invoice, order = big()
    exc = only(run([invoice], [order], [pay("P1", "INV-1", 90000, tds=2000)]))
    assert exc.facts["paid"] == 90000.0 and exc.facts["tds"] == 2000.0
    assert exc.amount_at_risk == 26000.0


def test_two_instalments_that_add_up_are_fine():
    invoice, order = big()
    payments = [pay("P1", "INV-1", 60000), pay("P2", "inv 001", 58000)]
    assert run([invoice], [order], payments).exceptions == []


def test_two_instalments_that_overshoot_are_an_overpayment():
    invoice, order = big()
    exc = only(run([invoice], [order], [pay("P1", "INV-1", 60000), pay("P2", "INV-1", 60000)]))
    assert exc.type == "overpayment" and exc.payment_ids == ["P1", "P2"]


# ---- payment linking (C3 step 4) ----

def test_blank_against_bill_links_when_one_invoice_of_that_vendor_matches_the_amount():
    result = run([inv("INV-1")], [po()], [pay("P1", "", 1180)])
    assert result.exceptions == [] and result.unmatched_payment_ids == []


def test_blank_against_bill_is_unmatched_when_the_amount_fits_no_invoice():
    result = run([inv("INV-1")], [po()], [pay("P1", "", 900)])
    assert result.unmatched_payment_ids == ["P1"]
    assert result.exceptions == []
    assert any("P1" in w for w in result.warnings)


def test_blank_against_bill_is_unmatched_when_two_invoices_fit():
    invoices = [inv("INV-1"), inv("INV-2", date="02/05/2024", po="PO-2")]
    result = run(invoices, [po(), po("PO-2")], [pay("P1", "", 1180)])
    assert result.unmatched_payment_ids == ["P1"]


def test_blank_against_bill_never_matches_another_vendors_invoice():
    result = run([inv("INV-1")], [po()], [pay("P1", "", 1180, payee="Somebody Else")])
    assert result.unmatched_payment_ids == ["P1"]


def test_payment_against_an_unknown_bill_is_unmatched_not_an_exception():
    result = run([inv("INV-1")], [po()], [pay("P1", "INV-77", 1180)])
    assert result.unmatched_payment_ids == ["P1"] and result.exceptions == []


def test_payment_to_a_duplicate_number_goes_to_the_original():
    result = run([inv("INV-1"), inv("INV-01", date="03/05/2024")], [po()], [pay("P1", "INV-1", 1180)])
    assert types(result) == ["duplicate_invoice"]
    assert result.unmatched_payment_ids == []


# ---- ordering, ids, totals ----

def test_exceptions_sort_by_risk_then_get_ids_in_order():
    invoices = [
        inv("INV-A", po="PO-404"),                              # missing_po, 1,180
        inv("INV-B", po="PO-2", rate=110),                      # amount_mismatch, 118
        inv("INV-C", po="PO-3", vendor="Sri Ganesh Steel"),     # vendor_name_mismatch, 0
        inv("INV-D", po="PO-404", qty=1000),                    # missing_po, 1,18,000
    ]
    pos = [po(), po("PO-2"), po("PO-3", vendor="Shree Ganesh Steels")]
    result = run(invoices, pos)
    assert [e.invoice_ids[0] for e in result.exceptions] == ["INV-D", "INV-A", "INV-B", "INV-C"]
    assert [e.id for e in result.exceptions] == ["EX-01", "EX-02", "EX-03", "EX-04"]
    assert result.amount_at_risk == pytest.approx(118000 + 1180 + 118)
    assert result.invoices_checked == 4


def test_ties_sort_by_severity_then_invoice_number():
    invoices = [inv("INV-B", po="PO-404", gstin=""), inv("INV-A", po="PO-404", gstin="")]
    result = run(invoices, [po()])
    # same amounts, so invoice number decides within a severity
    first_two = [(e.invoice_ids[0], e.type) for e in result.exceptions[:2]]
    assert first_two == [("INV-A", "missing_po"), ("INV-B", "missing_po")]


def test_facts_are_json_primitives_with_the_base_keys_and_no_ids():
    result = run([inv("INV-1", po="PO-404"), inv("INV-2", rate=110, po="PO-1")], [po()])
    for exc in result.exceptions:
        assert {"invoice_no", "vendor", "po_no"} <= set(exc.facts)
        assert all(isinstance(v, (str, int, float)) for v in exc.facts.values())
        json.dumps(exc.facts)
        assert "id" not in exc.facts and "source_rows" not in exc.facts


def test_money_facts_have_display_siblings():
    result = run([inv("INV-1", rate=110)], [po()])
    facts = only(result).facts
    for key in ("invoice_rate", "po_rate", "diff"):
        assert f"{key}_display" in facts


def test_amounts_are_rounded_to_two_places():
    exc = only(run([inv("INV-1", rate=110.333)], [po()]))
    assert exc.amount_at_risk == round(exc.amount_at_risk, 2)


# ---- entry points ----

def csv_files(invoices, pos, payments):
    def blob(header, lines):
        return ("\n".join([header, *lines]) + "\n").encode()
    return {
        "invoices": ("invoices.csv", blob(INVOICE_HEADER, invoices)),
        "purchase_orders": ("purchase_orders.csv", blob(PO_HEADER, pos)),
        "payments": ("payments.csv", blob(PAYMENT_HEADER, payments)),
    }


def test_triage_bytes_runs_the_whole_pipeline():
    result = triage_bytes(csv_files([inv("INV-1", rate=110)], [po()], [pay("P1", "INV-1", 1298)]))
    assert types(result) == ["amount_mismatch"]
    assert result.warnings == []


def test_triage_bytes_reports_loader_warnings():
    files = csv_files([inv("INV-1"), ",Ghost,,03/04/2024,,1,1,1,0,,1"], [po()], [pay("P1", "INV-1", 1180)])
    result = triage_bytes(files)
    assert len(result.warnings) == 1 and "invoices.csv row 3" in result.warnings[0]


def test_triage_bytes_names_a_missing_file():
    files = csv_files([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180)])
    del files["payments"]
    with pytest.raises(InputError, match="payments"):
        triage_bytes(files)


def test_triage_bytes_passes_loader_errors_through():
    files = csv_files([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180)])
    files["payments"] = ("payments.csv", b"")
    with pytest.raises(InputError, match="payments.csv is empty"):
        triage_bytes(files)


def test_triage_bytes_accepts_a_custom_tolerance():
    files = csv_files([inv("INV-1", rate=100.5)], [po()], [pay("P1", "INV-1", 1185.9)])
    assert triage_bytes(files, Tolerance(amount_pct=0, amount_abs=0, payment_abs=0)).exceptions
    assert triage_bytes(files).exceptions == []


def test_triage_dir_reads_the_three_sample_file_names(tmp_path):
    files = csv_files([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180)])
    for kind, (_, data) in files.items():
        (tmp_path / f"{kind}.csv").write_bytes(data)
    result = triage_dir(tmp_path)
    assert result.invoices_checked == 1 and result.exceptions == []


def test_triage_dir_names_a_missing_file(tmp_path):
    with pytest.raises(InputError, match="invoices.csv"):
        triage_dir(tmp_path)


def test_triage_dir_accepts_a_string_path(tmp_path):
    files = csv_files([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180)])
    for kind, (_, data) in files.items():
        (tmp_path / f"{kind}.csv").write_bytes(data)
    assert triage_dir(str(tmp_path)).invoices_checked == 1


# ---- the offline guard from conftest.py (AC7) ----

def test_guard_blocks_outside_network():
    with pytest.raises(RuntimeError, match="may not connect"):
        socket.socket().connect(("93.184.216.34", 80))


def test_guard_blocks_the_claude_cli():
    with pytest.raises(RuntimeError, match="claude"):
        subprocess.Popen(["/usr/local/bin/claude", "-p", "hi"])
    with pytest.raises(RuntimeError, match="claude"):
        subprocess.run("claude -p hi", shell=True)


def test_guard_allows_localhost_sockets():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    client.connect(server.getsockname())
    client.close()
    server.close()


# ---- fix round 1: messy-export realism ----

def test_gst_amount_check_is_skipped_when_taxable_value_is_blank():
    line = inv("INV-1", rate="", taxable="", gst_amount=180, total=1180)
    result = run([line], [po()], expect_warnings=True)
    assert result.exceptions == []


@pytest.mark.parametrize("placeholder", ["-", "NA", "N/A", "URD", "Unregistered"])
def test_gstin_placeholder_at_zero_gst_is_not_a_finding(placeholder):
    assert run([inv("INV-1", gstin=placeholder, gst=0)], [po(gst=0)]).exceptions == []


@pytest.mark.parametrize("placeholder", ["-", "NA", "N/A", "URD", "Unregistered"])
def test_gstin_placeholder_above_zero_gst_is_reported_as_missing(placeholder):
    assert gst_exc(inv("INV-1", gstin=placeholder)).facts["reason"] == "missing"


@pytest.mark.parametrize("placeholder", ["-", "NA", "N/A", "nil"])
def test_po_ref_placeholder_uses_the_single_po_fallback(placeholder):
    assert run([inv("INV-1", po=placeholder)], [po()]).exceptions == []


@pytest.mark.parametrize("placeholder", ["-", "NA", "N/A", "nil"])
def test_po_ref_placeholder_with_no_fallback_is_a_missing_po(placeholder):
    exc = only(run([inv("INV-1", po=placeholder)], [po(), po("PO-2")]))
    assert exc.type == "missing_po" and exc.facts["po_ref_given"] == ""


@pytest.mark.parametrize("placeholder", ["-", "NA", "N/A", "nil"])
def test_invoice_ref_placeholder_matches_the_payment_by_vendor_and_amount(placeholder):
    result = run([inv("INV-1")], [po()], [pay("P1", placeholder, 1180)])
    assert result.exceptions == [] and result.unmatched_payment_ids == []


# ---- council fix: ambiguous bill numbers, credit notes, repeated POs ----

def ambiguous_bills():
    """Bill K-1 exists for two vendors: Kumar Packaging (11,800) and Zenith Traders (1,180)."""
    return (
        [inv("K-1", vendor="Kumar Packaging", qty=10, rate=1000),
         inv("K-1", vendor="Zenith Traders", po="PO-2", qty=10, rate=10)],
        [po("PO-1", vendor="Kumar Packaging", rate=1000), po("PO-2", vendor="Zenith Traders", rate=10)],
    )


def test_shared_bill_number_links_to_the_vendor_with_the_closest_name():
    invoices, pos = ambiguous_bills()
    result = run(invoices, pos, [pay("P1", "K-1", 118, payee="Zenith Trading Co")])
    assert result.exceptions == [] and result.unmatched_payment_ids == []


def test_shared_bill_number_with_an_exact_vendor_match_still_links():
    invoices, pos = ambiguous_bills()
    result = run(invoices, pos, [pay("P1", "K-1", 118, payee="Zenith Traders Pvt Ltd")])
    assert result.exceptions == [] and result.unmatched_payment_ids == []


def test_shared_bill_number_with_no_matching_payee_is_left_unmatched_not_given_to_the_first():
    invoices, pos = ambiguous_bills()
    result = run(invoices, pos, [pay("P1", "K-1", 118, payee="Totally Different Ltd")])
    assert result.exceptions == []
    assert result.unmatched_payment_ids == ["P1"]
    assert "P1" in result.warnings[0] and "K-1" in result.warnings[0]


def test_shared_bill_number_with_a_tie_on_the_payee_name_is_left_unmatched():
    invoices = [inv("K-1", vendor="Sri Ganesh Steel"), inv("K-1", vendor="Sri Ganesh Foods", po="PO-2")]
    pos = [po("PO-1", vendor="Sri Ganesh Steel"), po("PO-2", vendor="Sri Ganesh Foods")]
    result = run(invoices, pos, [pay("P1", "K-1", 1180, payee="Sri Ganesh")])
    assert result.unmatched_payment_ids == ["P1"] and "K-1" in result.warnings[0]


def test_generic_words_alone_do_not_link_a_payee():
    invoices = [inv("K-1", vendor="Kumar Traders"), inv("K-1", vendor="Zenith Traders", po="PO-2")]
    pos = [po("PO-1", vendor="Kumar Traders"), po("PO-2", vendor="Zenith Traders")]
    result = run(invoices, pos, [pay("P1", "K-1", 1180, payee="Acme Traders")])
    assert result.unmatched_payment_ids == ["P1"]


def test_a_single_candidate_still_links_even_if_the_payee_name_differs():
    result = run([inv("INV-1")], [po()], [pay("P1", "INV-1", 1180, payee="Someone Else")])
    assert result.exceptions == [] and result.unmatched_payment_ids == []


def credit_note(**kwargs):
    defaults = dict(qty=10, rate=100, taxable=-1000, gst_amount=-180, total=-1180)
    return inv("CN-1", **(defaults | kwargs))


@pytest.mark.parametrize("line", [
    credit_note(),
    credit_note(total=1180),  # only the taxable value is negative
    credit_note(taxable=1000, gst_amount=180, total=-1180),  # only the total is negative
    credit_note(qty=-10, taxable=1000, gst_amount=180, total=1180),  # only the quantity is negative
], ids=["negative_total_and_taxable", "negative_taxable", "negative_total", "negative_qty"])
def test_credit_notes_are_skipped_with_a_warning(line):
    result = run([inv("INV-1", qty=10), line], [po()])
    assert result.exceptions == []
    assert result.warnings == ["Row 3 (CN-1) looks like a credit note and was not matched. "
                               "Credit notes are not supported yet."]
    assert result.invoices_checked == 1


def test_a_credit_note_does_not_use_up_the_po_quantity():
    result = run([inv("INV-1", qty=10), credit_note(qty=-5, taxable=-500, gst_amount=-90, total=-590)], [po()])
    assert result.exceptions == []


def test_a_credit_note_with_a_different_rate_is_not_an_amount_mismatch():
    result = run([credit_note(rate=999, taxable=-9990, gst_amount=-1798.2, total=-11788.2)], [po()])
    assert result.exceptions == []


def test_a_repeated_po_number_warns_and_uses_the_first_row():
    result = run([inv("INV-1")], [po("PO-1"), po("po 1", qty=999)])
    assert result.exceptions == []
    assert result.warnings == ["po 1 appears more than once; the first row is used."]
