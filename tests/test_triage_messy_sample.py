"""samples/messy_tally is a synthetic messy export with 6 planted problems; the checker finds those and nothing else."""
import csv
from pathlib import Path

import run as run_module
from triage.engine import triage_bytes

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "messy_tally"


def planted() -> set[tuple[str, str]]:
    with (SAMPLE / "expected_exceptions.csv").open(encoding="utf-8", newline="") as handle:
        return {(row["exception_type"], row["invoice_no"]) for row in csv.DictReader(handle)}


def test_the_six_planted_problems_are_found_and_nothing_else():
    result = triage_bytes(run_module.find_export_files(SAMPLE))
    found = {(exc.type, exc.invoice_ids[0].strip()) for exc in result.exceptions}
    assert len(planted()) == 6
    assert found == planted()
    assert result.invoices_checked == 25
    assert result.warnings == []


def test_check_dir_prints_the_six_findings(capsys):
    assert run_module.main(["--check", "--dir", str(SAMPLE)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "exceptions found: 6" in out
    assert "invoices checked: 25" in out
