"""The polish round: duplicates carry the original's rows, flagged cells, GST aware amount sentences, paid twice,
British spelling, and a drawer that always shows the amount columns."""
import csv
import json
import re
import threading
import urllib.request
from pathlib import Path

import pytest

from triage.engine import SAMPLE_FILES, triage_bytes, triage_dir
from triage.explain import cached_count, explain_all, fingerprint, is_valid_sentence, load_cache, offline_sentence
from triage.server import make_server

DEMO_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = DEMO_DIR / "sample_data"
INDEX_HTML = DEMO_DIR / "triage" / "web" / "index.html"
AMERICAN_SPELLING = re.compile(r"\b\w*(?:authoriz|organiz|recogniz|categoriz|prioritiz|analyz|summariz)\w*", re.I)


@pytest.fixture(scope="module")
def result():
    return triage_dir(SAMPLE_DIR)


def by_invoice(result, invoice_no, kind=None):
    found = [e for e in result.exceptions if e.invoice_ids[0] == invoice_no and (kind is None or e.type == kind)]
    assert len(found) == 1, found
    return found[0]


def cells(exc):
    return {(c["kind"], c["row"], c["field"]) for c in exc.flagged_cells}


# ---- 1. a duplicate carries the original's PO and payments ----

@pytest.mark.parametrize("dup, original, po, payment", [
    ("INV/42", "INV-0042", "PO-00108", "PV/24-25/0102"),
    ("SE-24-25-107", "SE/24-25/0107", "PO-00129", "PV/24-25/0106"),
])
def test_duplicate_carries_the_originals_po_and_payments(result, dup, original, po, payment):
    exc = by_invoice(result, dup, "duplicate_invoice")
    assert exc.invoice_ids == [dup, original]
    assert exc.po_ids == [po] and exc.payment_ids == [payment]
    assert len(exc.source_rows["purchase_orders"]) == 1 and len(exc.source_rows["payments"]) == 1
    assert exc.facts["po_no"] == po
    assert exc.amount_at_risk > 0  # the risk is still the duplicate's total, not changed by the extra rows


def test_the_answer_key_lists_the_originals_po_and_payments_for_duplicates():
    rows = list(csv.DictReader((SAMPLE_DIR / "expected_exceptions.csv").open(encoding="utf-8")))
    dups = {r["invoice_no"]: r for r in rows if r["exception_type"] == "duplicate_invoice"}
    assert dups["INV/42"]["po_no"] == "PO-00108" and dups["INV/42"]["payment_refs"] == "PV/24-25/0102"
    assert dups["SE-24-25-107"]["po_no"] == "PO-00129" and dups["SE-24-25-107"]["payment_refs"] == "PV/24-25/0106"


# ---- 3. flagged cells ----

def test_duplicate_flags_the_total_on_both_copies(result):
    exc = by_invoice(result, "INV/42")
    assert cells(exc) == {("invoices", 17, "total"), ("invoices", 6, "total")}


def test_amount_mismatch_flags_the_invoice_rate_and_the_po_rate(result):
    exc = by_invoice(result, "GAC/118", "amount_mismatch")
    assert cells(exc) == {("invoices", 13, "rate"), ("purchase_orders", 10, "rate")}


def test_amount_mismatch_flags_taxable_value_when_the_invoice_has_no_rate_cell():
    gstin = "27AAPFU0939F1ZV"
    files = {
        "invoices": ("invoices.csv", (
            "Inv No,Supplier,GSTIN,Invoice Date,PO Ref,Qty,Unit Price,Taxable Amt,GST Rate,GST Amount,Total\n"
            f"INV-1,Acme Traders,{gstin},01/05/2024,PO-1,10,,1200,18,216,1416\n").encode()),
        "purchase_orders": ("purchase_orders.csv", (
            "Order Number,Supplier,Qty Ordered,Price,GST %,GSTIN\n"
            f"PO-1,Acme Traders,10,100,18,{gstin}\n").encode()),
        "payments": ("payments.csv", b"Payment No,Beneficiary,Bill Ref,Amount,TDS\nP1,Acme Traders,INV-9,1,0\n"),
    }
    exc = by_invoice(triage_bytes(files), "INV-1", "amount_mismatch")
    assert cells(exc) == {("invoices", 2, "taxable_value"), ("purchase_orders", 2, "rate")}


def test_quantity_mismatch_flags_the_quantities(result):
    exc = by_invoice(result, "BR/24/2330", "quantity_mismatch")
    kinds = {(kind, field) for kind, _, field in cells(exc)}
    assert kinds == {("invoices", "qty"), ("purchase_orders", "qty")}
    assert ("invoices", exc.source_rows["invoices"][0], "qty") in cells(exc)


@pytest.mark.parametrize("invoice_no, expected_fields", [
    ("INV-0215", {("invoices", "vendor_gstin")}),                                  # bad checksum
    ("HL/2425/231", {("invoices", "vendor_gstin")}),                               # bad format
    ("OSE/2024/338", {("invoices", "gst_rate"), ("purchase_orders", "gst_rate")}),  # rate against the PO
])
def test_gst_errors_flag_the_gstin_or_the_rates(result, invoice_no, expected_fields):
    exc = by_invoice(result, invoice_no, "gst_error")
    assert {(kind, field) for kind, _, field in cells(exc)} == expected_fields


@pytest.mark.parametrize("invoice_no, kind", [("SGS/0455", "overpayment"), ("AP/0925", "partial_payment")])
def test_payment_issues_flag_the_paid_amount_and_the_invoice_total(result, invoice_no, kind):
    exc = by_invoice(result, invoice_no, kind)
    fields = {(k, field) for k, _, field in cells(exc)}
    assert fields == {("payments", "amount_paid"), ("invoices", "total")}
    flagged_payment_rows = {row for k, row, _ in cells(exc) if k == "payments"}
    assert flagged_payment_rows == set(exc.source_rows["payments"])


def test_missing_po_flags_the_order_reference(result):
    exc = by_invoice(result, "AS/0071", "missing_po")
    assert cells(exc) == {("invoices", exc.source_rows["invoices"][0], "po_ref")}


def test_vendor_mismatch_flags_both_vendor_names(result):
    exc = by_invoice(result, "SGS/0412", "vendor_name_mismatch")
    assert {(k, f) for k, _, f in cells(exc)} == {("invoices", "vendor"), ("purchase_orders", "vendor")}


def test_every_flagged_cell_points_at_a_row_the_exception_lists(result):
    for exc in result.exceptions:
        assert exc.flagged_cells, exc.id
        for flag in exc.flagged_cells:
            assert flag["row"] in exc.source_rows[flag["kind"]], (exc.id, flag)


# ---- 6. amount mismatch sentences reconcile with the amount at risk ----

@pytest.mark.parametrize("invoice_no, difference, at_risk", [("GAC/118", "₹14,000", "₹16,520"),
                                                             ("MTW/1043", "₹3,000", "₹3,540")])
def test_amount_mismatch_sentence_says_the_risk_includes_gst(result, invoice_no, difference, at_risk):
    exc = by_invoice(result, invoice_no, "amount_mismatch")
    text = offline_sentence(exc)
    assert difference in text and at_risk in text and "18% GST" in text
    assert is_valid_sentence(text, exc.facts, exc.amount_at_risk)


def test_amount_mismatch_without_gst_keeps_the_plain_sentence(result):
    exc = by_invoice(result, "GAC/118", "amount_mismatch")
    plain = type(exc)(**{**exc.__dict__, "facts": {**exc.facts, "gst_rate": 0.0}, "amount_at_risk": 14000.0})
    assert "GST" not in offline_sentence(plain)


def test_a_model_sentence_that_hides_the_gst_is_rejected(result):
    exc = by_invoice(result, "GAC/118", "amount_mismatch")
    old = "Invoice GAC/118 bills ₹668 per unit against the agreed PO-00127 rate of ₹640, overcharging ₹14,000."
    assert not is_valid_sentence(old, exc.facts, exc.amount_at_risk)
    good = ("Invoice GAC/118 bills ₹668 per unit against ₹640 on PO-00127, which is ₹14,000 over the agreed price "
            "and ₹16,520 once 18% GST is added.")
    assert is_valid_sentence(good, exc.facts, exc.amount_at_risk)


# ---- 7. paid twice ----

def test_an_invoice_paid_exactly_twice_says_so(result):
    exc = by_invoice(result, "SGS/0455", "overpayment")
    assert exc.facts["paid_twice"] is True
    text = offline_sentence(exc)
    assert "paid twice" in text and "₹87,320" in text and "₹1,74,640" in text
    assert is_valid_sentence(text, exc.facts, exc.amount_at_risk)


def test_other_overpayments_are_not_called_paid_twice(result):
    exc = by_invoice(result, "SGS/0455", "overpayment")
    other = type(exc)(**{**exc.__dict__, "facts": {k: v for k, v in exc.facts.items() if k != "paid_twice"}})
    assert "twice" not in offline_sentence(other)


def test_a_model_sentence_for_a_double_payment_must_say_twice(result):
    exc = by_invoice(result, "SGS/0455", "overpayment")
    vague = "Invoice SGS/0455 is overpaid: payments of ₹1,74,640 exceed the ₹87,320 invoice total."
    assert not is_valid_sentence(vague, exc.facts, exc.amount_at_risk)


# ---- 8. British spelling ----

def test_every_offline_sentence_uses_british_spelling(result):
    for exc in result.exceptions:
        assert not AMERICAN_SPELLING.search(offline_sentence(exc)), offline_sentence(exc)


def test_a_model_sentence_with_american_spelling_is_rejected(result):
    exc = by_invoice(result, "AS/0071", "missing_po")
    text = "Invoice AS/0071 from Apex Safety Products has no PO, so ₹1,29,800 is at risk without authorization."
    assert not is_valid_sentence(text, exc.facts, exc.amount_at_risk)
    assert is_valid_sentence(text.replace("authorization", "authorisation"), exc.facts, exc.amount_at_risk)


def test_the_shipped_files_use_british_spelling():
    for path in [*DEMO_DIR.glob("*.md"), INDEX_HTML, SAMPLE_DIR / "explanations_cache.json"]:
        assert not AMERICAN_SPELLING.search(path.read_text(encoding="utf-8")), path.name


# ---- the committed cache still covers every sample exception ----

def test_every_sample_exception_has_a_valid_cached_sentence(result):
    assert cached_count(result.exceptions) == len(result.exceptions) == 16
    texts = explain_all(result.exceptions)  # offline backend: cache hits only
    entries = load_cache(SAMPLE_DIR / "explanations_cache.json")
    for exc in result.exceptions:
        assert texts[exc.id] == entries[fingerprint(exc)]["text"]


# ---- the API: row order, labels, flagged headers, amount columns first ----

@pytest.fixture(scope="module")
def sample_json():
    server = make_server("127.0.0.1", 0, SAMPLE_DIR, lambda excs: {e.id: "x." for e in excs})
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    with urllib.request.urlopen(f"http://{host}:{port}/api/sample", timeout=20) as resp:
        data = json.loads(resp.read())
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    return data


def api_exception(data, invoice_no, kind):
    return next(e for e in data["exceptions"] if e["invoice_no"] == invoice_no and e["type"] == kind)


def test_a_duplicate_shows_the_original_row_first_then_the_duplicate(sample_json):
    exc = api_exception(sample_json, "INV/42", "duplicate_invoice")
    rows = exc["rows"]["invoices"]
    assert [r["row"] for r in rows] == [6, 17]
    assert [r["label"] for r in rows] == ["Original", "Duplicate"]
    assert rows[0]["cells"]["Bill Number"] == "INV-0042" and rows[1]["cells"]["Bill Number"] == "INV/42"
    assert [r["row"] for r in exc["rows"]["purchase_orders"]] == [exc["rows"]["purchase_orders"][0]["row"]]
    assert exc["rows"]["payments"][0]["cells"]["Payment Voucher"] == "PV/24-25/0102"
    assert "original" in exc["notes"]["payments"].lower() and "original" in exc["notes"]["purchase_orders"].lower()


def test_flagged_cells_come_back_as_the_users_own_column_headers(sample_json):
    exc = api_exception(sample_json, "GAC/118", "amount_mismatch")
    assert exc["rows"]["invoices"][0]["flagged"] == ["Rate"]
    assert exc["rows"]["purchase_orders"][0]["flagged"] == ["Unit Price"]
    dup = api_exception(sample_json, "INV/42", "duplicate_invoice")
    assert [r["flagged"] for r in dup["rows"]["invoices"]] == [["Invoice Amount"], ["Invoice Amount"]]
    paid = api_exception(sample_json, "SGS/0455", "overpayment")
    assert all(r["flagged"] == ["Amount Paid"] for r in paid["rows"]["payments"])
    assert paid["rows"]["invoices"][0]["flagged"] == ["Invoice Amount"]


def test_invoice_columns_lead_with_bill_number_date_and_invoice_total(sample_json):
    columns = sample_json["files"]["invoices"]["columns"]
    assert columns[:3] == ["Bill Number", "Date", "Invoice Amount"]
    assert columns[3:] == [h for h in sample_json["files"]["invoices"]["header"] if h not in columns[:3]]


def test_payments_and_po_columns_still_lead_with_identity_then_amounts(sample_json):
    files = sample_json["files"]
    assert files["payments"]["columns"][:5] == ["Payment Voucher", "Paid To", "Against Bill", "Amount Paid",
                                                 "TDS Deducted"]
    assert files["purchase_orders"]["columns"][:6] == ["PO#", "Vendor Name", "Quantity", "Unit Price", "Sub Total",
                                                        "Tax %"]


def test_amount_columns_are_still_marked_and_nothing_is_dropped(sample_json):
    files = sample_json["files"]
    assert set(files["invoices"]["amount_columns"]) == {"Billed Qty", "Rate", "Taxable Value", "GST %",
                                                        "Tax Amount", "Invoice Amount"}
    assert {"Amount Paid", "TDS Deducted"} <= set(files["payments"]["amount_columns"])
    for kind in files:
        assert sorted(files[kind]["columns"]) == sorted(files[kind]["header"])


# ---- the page: no ellipsis, a scroll cue, amount columns marked ----

def test_the_page_never_truncates_text_with_an_ellipsis():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "line-clamp" not in html and "text-overflow" not in html and "ellipsis" not in html


def test_the_page_has_a_scroll_cue_for_wide_drawer_tables():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "scroll-cue" in html and "amount_columns" in html and "flagged" in html


def test_flagged_cells_get_a_strong_tint_and_flagged_columns_follow_the_lead_columns():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "--flag-bg" in html and re.search(r"\.mini td\.flag\s*\{[^}]*background:\s*var\(--flag-bg\)", html)
    assert "flaggedFirst" in html
    assert set(SAMPLE_FILES) <= {"invoices", "purchase_orders", "payments"}
