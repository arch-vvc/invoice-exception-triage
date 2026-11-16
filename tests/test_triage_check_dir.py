"""`python run.py --check --dir <folder>` checks a customer's own exports from the terminal."""
import csv
import shutil
from pathlib import Path

import pytest

import run as run_module  # run.py sits in the demo folder, which conftest puts on sys.path

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"
SAMPLE_CSVS = ("invoices.csv", "purchase_orders.csv", "payments.csv")


@pytest.fixture
def folder(tmp_path):
    for name in SAMPLE_CSVS:
        shutil.copy(SAMPLE_DIR / name, tmp_path / name)
    return tmp_path


def check_dir(capsys, *args):
    code = run_module.main(["--check", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_sample_copy_gives_16_exceptions_and_no_answer_key_line(folder, capsys):
    code, out, _ = check_dir(capsys, "--dir", str(folder))
    assert code == 0
    assert "exceptions found: 16" in out.splitlines()
    assert "invoices checked: 40" in out.splitlines()
    assert "answer key" not in out
    assert "Largest findings" in out


def test_prints_the_five_largest_findings_with_a_sentence(folder, capsys):
    _, out, _ = check_dir(capsys, "--dir", str(folder))
    block = out.split("Largest findings", 1)[1].strip().splitlines()[1:]
    assert len(block) == 5
    assert all("₹" in line for line in block)


def test_missing_payments_file_exits_2_and_names_payments(folder, capsys):
    (folder / "payments.csv").unlink()
    code, out, err = check_dir(capsys, "--dir", str(folder))
    assert code == 2
    assert "payments" in out + err
    assert "invoices.csv" in out + err  # the names it did see


def test_two_invoice_files_exit_2_and_list_both(folder, capsys):
    shutil.copy(folder / "invoices.csv", folder / "Invoices old.csv")
    code, out, err = check_dir(capsys, "--dir", str(folder))
    assert code == 2
    text = out + err
    assert "invoices.csv" in text and "Invoices old.csv" in text


def test_missing_folder_exits_2(tmp_path, capsys):
    code, out, err = check_dir(capsys, "--dir", str(tmp_path / "nope"))
    assert code == 2
    assert "nope" in out + err


def test_clean_set_says_no_exceptions_found_and_exits_0(tmp_path, capsys):
    (tmp_path / "Purchase Register.csv").write_text(
        "Inv No,Supplier,GSTIN,Invoice Date,PO Ref,Qty,Unit Price,Taxable Amt,GST Rate,GST Amount,Total\n"
        'INV-1,"Acme Traders",27ABCDE1234F1Z0,01/05/2024,PO-1,10,100,1000,18,180,1180\n', encoding="utf-8")
    (tmp_path / "PO list.csv").write_text(
        "Order Number,Supplier,Qty Ordered,Price,GST %,GSTIN\n"
        'PO-1,"Acme Traders",10,100,18,27ABCDE1234F1Z0\n', encoding="utf-8")
    (tmp_path / "Vendor Payments.csv").write_text(
        "Payment No,Beneficiary,Bill Ref,Amount,TDS\n"
        'PAY-1,"Acme Traders",INV-1,1180,0\n', encoding="utf-8")
    code, out, _ = check_dir(capsys, "--dir", str(tmp_path))
    assert code == 0
    assert "no exceptions found" in out
    assert "Largest findings" not in out


def test_csv_option_writes_the_full_report(folder, tmp_path_factory, capsys):
    target = tmp_path_factory.mktemp("out") / "report.csv"
    code, _, _ = check_dir(capsys, "--dir", str(folder), "--csv", str(target))
    assert code == 0
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 1 + 16


def test_check_without_dir_still_compares_with_the_answer_key(capsys):
    code, out, _ = check_dir(capsys)
    assert code == 0
    assert out.splitlines()[-2] == "answer key: 16/16 matched, 0 extra"


def test_dir_and_csv_need_check():
    with pytest.raises(SystemExit):
        run_module.parse_args(["--dir", "x"])
    with pytest.raises(SystemExit):
        run_module.parse_args(["--csv", "x"])
