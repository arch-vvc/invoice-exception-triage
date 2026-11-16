"""The headline amount counts each invoice once (its largest finding); the plain sum stays available."""
import subprocess
import sys
from pathlib import Path

import pytest

from triage.engine import TriageException, triage_dir, unique_amount_at_risk

DEMO_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = DEMO_DIR / "sample_data"


def finding(row, risk, kind="missing_po"):
    return TriageException(
        id="", type=kind, severity="high", invoice_ids=[f"INV-{row}"], po_ids=[], payment_ids=[],
        source_rows={"invoices": [row], "purchase_orders": [], "payments": []},
        amount_at_risk=risk, suggested_action="x", facts={},
    )


def test_one_invoice_with_two_findings_counts_the_larger_one():
    found = [finding(2, 1000.0), finding(2, 250.0, "gst_error")]
    assert unique_amount_at_risk(found) == 1000.0
    assert round(sum(e.amount_at_risk for e in found), 2) == 1250.0


def test_two_invoices_with_one_finding_each_unique_equals_sum():
    found = [finding(2, 1000.0), finding(3, 250.5)]
    assert unique_amount_at_risk(found) == 1250.5


def test_no_findings_both_zero():
    assert unique_amount_at_risk([]) == 0.0


def test_finding_without_an_invoice_counts_on_its_own():
    orphan = finding(2, 40.0)
    orphan.source_rows = {"invoices": [], "purchase_orders": [], "payments": []}
    orphan2 = finding(2, 60.0)
    orphan2.source_rows = {"invoices": [], "purchase_orders": [], "payments": []}
    assert unique_amount_at_risk([orphan, orphan2, finding(5, 10.0)]) == 110.0


def test_sample_data_unique_is_at_most_the_sum():
    result = triage_dir(SAMPLE_DIR)
    assert result.amount_at_risk_findings == pytest.approx(sum(e.amount_at_risk for e in result.exceptions), abs=0.01)
    assert 0 < result.amount_at_risk <= result.amount_at_risk_findings


def test_check_prints_both_figures_for_the_sample_data():
    out = subprocess.run([sys.executable, str(DEMO_DIR / "run.py"), "--check"], capture_output=True, text=True,
                         encoding="utf-8", check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert any(line.startswith("amount at risk: ₹") for line in lines)
    assert any(line.startswith("across all findings: ₹") for line in lines)
    assert "answer key: 16/16 matched, 0 extra" in lines
