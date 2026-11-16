"""The downloadable exception report, as CSV that opens cleanly in Excel."""
import csv
import io

from .engine import TriageException

HEADER = ["Exception", "Severity", "Invoice", "Vendor", "PO", "Amount at risk (₹)", "What went wrong",
          "Suggested action", "Source rows"]
TYPE_LABELS = {
    "duplicate_invoice": "Duplicate invoice",
    "missing_po": "Missing PO",
    "vendor_name_mismatch": "Vendor name mismatch",
    "amount_mismatch": "Amount mismatch",
    "quantity_mismatch": "Quantity mismatch",
    "gst_error": "GST error",
    "partial_payment": "Partial payment",
    "overpayment": "Overpayment",
}
FILE_LABELS = {"invoices": "Invoices", "purchase_orders": "Purchase orders", "payments": "Payments"}
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(text: str) -> str:
    """Stop Excel running a vendor name or note that starts like a formula."""
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def source_rows_text(source_rows: dict[str, list[int]]) -> str:
    """'Invoices row 17, 6; Payments row 4' - the file lines to open, one group per file."""
    groups = [f"{label} row {', '.join(str(n) for n in source_rows[kind])}"
              for kind, label in FILE_LABELS.items() if source_rows.get(kind)]
    return "; ".join(groups)


def plain(value) -> str:
    return "" if value is None else str(value)


def to_csv(exceptions: list[TriageException], explanations: dict[str, str]) -> str:
    """One row per exception, in the order given, in plain words. No byte order mark: see to_csv_bytes."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for exc in exceptions:
        writer.writerow([
            TYPE_LABELS.get(exc.type, exc.type.replace("_", " ").capitalize()),
            exc.severity.capitalize(),
            safe_cell(plain(exc.facts.get("invoice_no", exc.invoice_ids[0] if exc.invoice_ids else ""))),
            safe_cell(plain(exc.facts.get("vendor", ""))),
            safe_cell(plain(exc.facts.get("po_no", ""))),
            f"{exc.amount_at_risk:.2f}",
            safe_cell(explanations.get(exc.id, "")),
            exc.suggested_action,
            source_rows_text(exc.source_rows),
        ])
    return out.getvalue()


def to_csv_bytes(exceptions: list[TriageException], explanations: dict[str, str]) -> bytes:
    """The file the dashboard downloads and `--csv` writes: UTF-8 with a BOM, so Excel shows the rupee sign."""
    return b"\xef\xbb\xbf" + to_csv(exceptions, explanations).encode("utf-8")
