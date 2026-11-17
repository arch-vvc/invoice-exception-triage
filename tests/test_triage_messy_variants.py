"""`--check --dir` copes with the ways accounting exports differ (fixtures in tests/fixtures/messy/).

The fixtures are written from general knowledge of Tally and Zoho style exports, not from a real export.
"""
import shutil
from pathlib import Path

import pytest

import run as run_module

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "messy"
VARIANTS = ["baseline", "aliases", "dash_dates", "mon_dates", "rupee", "bom", "cp1252", "blank_lines", "grand_total"]


def check_variant(variant, tmp_path, capsys):
    for name in ("purchase_orders.csv", "payments.csv"):
        shutil.copy(FIXTURES / "common" / name, tmp_path / name)
    shutil.copy(FIXTURES / f"invoices_{variant}.csv", tmp_path / "invoices.csv")
    code = run_module.main(["--check", "--dir", str(tmp_path)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_reads_as_three_clean_invoices(variant, tmp_path, capsys):
    code, out, err = check_variant(variant, tmp_path, capsys)
    assert err == ""
    assert code == 0
    lines = out.splitlines()
    assert "invoices checked: 3" in lines
    assert "exceptions found: 0" in lines
    assert not [line for line in lines if line.startswith("warning:")]


def test_missing_columns_print_a_plain_message_and_exit_2(tmp_path, capsys):
    code, out, err = check_variant("missing_column", tmp_path, capsys)
    assert code == 2
    assert out == ""
    assert "Traceback" not in err
    assert "invoices.csv" in err
    assert "missing a column for" in err
    assert "found:" in err and "Taxable Value" in err  # the columns it did find
    assert "We looked for:" in err  # the columns it needed


def test_unreadable_bytes_exit_2_without_a_traceback(tmp_path, capsys):
    for name in ("purchase_orders.csv", "payments.csv"):
        shutil.copy(FIXTURES / "common" / name, tmp_path / name)
    (tmp_path / "invoices.csv").write_bytes(b"\x00\x01\x02\xff\xfe\x00junk")
    code = run_module.main(["--check", "--dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err
    assert "invoices.csv" in err
