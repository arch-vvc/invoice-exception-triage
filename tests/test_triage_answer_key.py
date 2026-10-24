"""The engine against the hand-built answer key (contract C6).

expected_exceptions.csv is written by generate.py from its own arithmetic and never from the engine.
If one of these tests fails, find out which side breaks the contract. Do not edit the key to match the engine.
"""
import csv
from collections import Counter
from pathlib import Path

import pytest

from triage.engine import Tolerance, triage_dir

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
EXCEPTION_TYPES = [
    "duplicate_invoice", "missing_po", "vendor_name_mismatch", "amount_mismatch",
    "quantity_mismatch", "gst_error", "partial_payment", "overpayment",
]


def load_key() -> dict[tuple[str, str], dict[str, str]]:
    with open(SAMPLE_DIR / "expected_exceptions.csv", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    key = {(row["exception_type"], row["invoice_no"].strip()): row for row in rows}
    assert len(key) == len(rows), "the answer key lists the same (type, invoice_no) twice"
    return key


def pairs(result) -> list[tuple[str, str]]:
    return [(exc.type, exc.invoice_ids[0].strip()) for exc in result.exceptions]


@pytest.fixture(scope="module")
def result():
    return triage_dir(SAMPLE_DIR)


@pytest.fixture(scope="module")
def key():
    return load_key()


def test_engine_and_key_flag_the_same_exceptions(result, key):
    found = set(pairs(result))
    assert len(found) == len(result.exceptions), "the engine reported the same (type, invoice_no) twice"
    assert found - set(key) == set(), "engine found exceptions that are not in the answer key"
    assert set(key) - found == set(), "engine missed exceptions that are in the answer key"


def test_amounts_severity_and_invoice_row_match_the_key(result, key):
    problems = []
    for exc in result.exceptions:
        expected = key[(exc.type, exc.invoice_ids[0].strip())]
        label = f"{exc.type} {exc.invoice_ids[0]}"
        if abs(exc.amount_at_risk - float(expected["amount_at_risk"])) > 1.0:
            problems.append(f"{label}: amount {exc.amount_at_risk} vs key {expected['amount_at_risk']}")
        if exc.severity != expected["severity"]:
            problems.append(f"{label}: severity {exc.severity} vs key {expected['severity']}")
        if exc.source_rows["invoices"][0] != int(expected["invoice_row"]):
            problems.append(f"{label}: row {exc.source_rows['invoices'][0]} vs key {expected['invoice_row']}")
    assert problems == []


def test_payments_and_po_refs_match_the_key(result, key):
    problems = []
    for exc in result.exceptions:
        expected = key[(exc.type, exc.invoice_ids[0].strip())]
        key_payments = [p for p in expected["payment_refs"].split(";") if p]
        if sorted(exc.payment_ids) != sorted(key_payments):
            problems.append(f"{exc.type} {exc.invoice_ids[0]}: payments {exc.payment_ids} vs key {key_payments}")
        if expected["po_no"] and exc.po_ids != [expected["po_no"]]:
            problems.append(f"{exc.type} {exc.invoice_ids[0]}: PO {exc.po_ids} vs key {expected['po_no']}")
    assert problems == []


def test_no_warnings_and_no_unmatched_payments(result):
    assert result.warnings == []
    assert result.unmatched_payment_ids == []


def test_every_invoice_is_checked(result):
    assert result.invoices_checked == 40
    assert len(result.invoices) == 40


def test_totals_add_up(result):
    assert result.amount_at_risk == pytest.approx(sum(e.amount_at_risk for e in result.exceptions), abs=0.01)
    key_total = sum(float(row["amount_at_risk"]) for row in load_key().values())
    assert result.amount_at_risk == pytest.approx(key_total, abs=16 * 1.0)  # Rs 1 allowed on each of the 16


@pytest.mark.parametrize("exception_type", EXCEPTION_TYPES)
def test_each_exception_type_appears(result, exception_type):
    assert exception_type in {exc.type for exc in result.exceptions}


def test_ids_are_sequential_and_sorted_by_risk(result):
    assert [e.id for e in result.exceptions] == [f"EX-{n:02d}" for n in range(1, len(result.exceptions) + 1)]
    risks = [e.amount_at_risk for e in result.exceptions]
    assert risks == sorted(risks, reverse=True)


def test_clean_invoices_produce_no_exception(result, key):
    """A row that is neither in the key nor the first copy of a duplicate must not be the primary of anything."""
    key_rows = {int(row["invoice_row"]) for row in key.values()}
    originals = {inv.source_row for inv in result.invoices
                 if inv.invoice_no in {e.invoice_ids[1] for e in result.exceptions if e.type == "duplicate_invoice"}}
    flagged_rows = Counter(exc.source_rows["invoices"][0] for exc in result.exceptions)
    clean = [inv for inv in result.invoices if inv.source_row not in key_rows | originals]
    assert len(clean) >= 20, "expected most of the sample to be clean"
    assert [(inv.source_row, inv.invoice_no) for inv in clean if flagged_rows[inv.source_row]] == []


def test_two_runs_give_identical_exceptions_including_ids():
    first, second = triage_dir(SAMPLE_DIR), triage_dir(SAMPLE_DIR)
    assert first.exceptions == second.exceptions
    assert first.warnings == second.warnings


# ---- tolerance is configurable ----

def types_by_invoice(result) -> set[tuple[str, str]]:
    return set(pairs(result))


def test_default_tolerance_flags_both_amount_mismatches(result, key):
    amount_keys = {k for k in key if k[0] == "amount_mismatch"}
    assert len(amount_keys) == 2
    assert amount_keys <= types_by_invoice(result)


def test_wider_percent_tolerance_clears_the_amount_mismatches_only(key):
    loose = triage_dir(SAMPLE_DIR, Tolerance(amount_pct=0.10))
    expected_left = set(key) - {k for k in key if k[0] == "amount_mismatch"}
    assert types_by_invoice(loose) == expected_left


def test_large_absolute_tolerance_clears_the_amount_mismatches(key):
    loose = triage_dir(SAMPLE_DIR, Tolerance(amount_abs=1_000_000.0))
    left = types_by_invoice(loose)
    assert not {k for k in left if k[0] == "amount_mismatch"}
    # Everything that does not rest on the amount tolerance is still found.
    stable = {k for k in key if k[0] not in {"amount_mismatch", "gst_error", "partial_payment", "overpayment"}}
    assert stable <= left


def test_tight_tolerance_still_finds_everything_in_the_key(key):
    tight = triage_dir(SAMPLE_DIR, Tolerance(amount_pct=0.0, amount_abs=0.0))
    assert set(key) <= types_by_invoice(tight)


def test_large_payment_tolerance_clears_payment_exceptions(key):
    loose = triage_dir(SAMPLE_DIR, Tolerance(payment_abs=10_000_000.0))
    left = types_by_invoice(loose)
    assert not {k for k in left if k[0] in {"partial_payment", "overpayment"}}
    assert {k for k in key if k[0] not in {"partial_payment", "overpayment"}} <= left


def test_quantity_tolerance_can_absorb_a_small_overrun(key):
    loose = triage_dir(SAMPLE_DIR, Tolerance(qty_abs=1_000_000.0))
    assert not {k for k in types_by_invoice(loose) if k[0] == "quantity_mismatch"}
