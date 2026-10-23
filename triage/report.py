"""The downloadable exception report, as CSV text that opens cleanly in Excel."""
import csv
import io

from .engine import TriageException

HEADER = ["id", "type", "severity", "invoice_no", "vendor", "po_no", "payment_ids", "amount_at_risk",
          "suggested_action", "explanation", "source_rows"]
FILE_ORDER = ("invoices", "purchase_orders", "payments")
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(text: str) -> str:
    """Stop Excel running a vendor name or note that starts like a formula."""
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def source_rows_text(source_rows: dict[str, list[int]]) -> str:
    """'invoices:17,5; payments:3' - the file lines to open, one group per file."""
    groups = [f"{kind}:{','.join(str(n) for n in source_rows[kind])}" for kind in FILE_ORDER if source_rows.get(kind)]
    return "; ".join(groups)


def to_csv(exceptions: list[TriageException], explanations: dict[str, str]) -> str:
    """One row per exception, in the order given. The caller adds a UTF-8 byte order mark when sending a file."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for exc in exceptions:
        writer.writerow([
            exc.id,
            exc.type,
            exc.severity,
            safe_cell(str(exc.facts.get("invoice_no", exc.invoice_ids[0] if exc.invoice_ids else ""))),
            safe_cell(str(exc.facts.get("vendor", ""))),
            safe_cell(str(exc.facts.get("po_no", ""))),
            safe_cell(";".join(exc.payment_ids)),
            f"{exc.amount_at_risk:.2f}",
            exc.suggested_action,
            safe_cell(explanations.get(exc.id, "")),
            source_rows_text(exc.source_rows),
        ])
    return out.getvalue()
