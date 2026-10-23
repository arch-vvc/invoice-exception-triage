"""Read a messy CSV export into a Table: find the header, map columns by alias, skip preamble and footers."""
import csv
import io
import re
from dataclasses import dataclass, field

MAX_HEADER_LINE = 10
MIN_HEADER_MATCHES = 3
EXCEL_HINT = "In Excel use Save As > CSV UTF-8."


class InputError(ValueError):
    """A problem with an uploaded file, worded for the person who uploaded it."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str  # plain English, used in error messages
    required: bool
    aliases: tuple[str, ...]


def _f(name: str, label: str, required: bool, *aliases: str) -> Field:
    return Field(name, label, required, aliases)


SCHEMAS: dict[str, tuple[Field, ...]] = {
    "invoices": (
        _f("invoice_no", "invoice number", True, "Invoice No", "Inv No", "Bill Number", "Bill No", "Voucher No",
           "Invoice Number", "Supplier Invoice No"),
        _f("vendor", "vendor name", True, "Party Name", "Vendor", "Vendor Name", "Supplier", "Supplier Name", "Party"),
        _f("vendor_gstin", "vendor GSTIN", True, "GSTIN/UIN", "GSTIN", "Party GSTIN", "Vendor GSTIN", "GST No"),
        _f("invoice_date", "invoice date", True, "Date", "Invoice Date", "Bill Date", "Voucher Date"),
        _f("po_ref", "PO reference", True, "Order No", "PO No", "PO Number", "PO Ref", "Purchase Order", "Order Ref"),
        _f("item", "item", False, "Particulars", "Item", "Item Name", "Description"),
        _f("qty", "quantity", True, "Billed Qty", "Qty", "Quantity"),
        _f("rate", "rate", False, "Rate", "Unit Price", "Price"),
        _f("taxable_value", "taxable value", True, "Taxable Value", "Taxable Amt", "Assessable Value", "Sub Total"),
        _f("gst_rate", "GST rate", True, "GST %", "GST Rate", "Tax Rate", "Tax %"),
        _f("gst_amount", "GST amount", False, "Tax Amount", "GST Amount", "Total Tax"),
        _f("total", "invoice total", True, "Invoice Amount", "Total", "Grand Total", "Bill Amount", "Invoice Total"),
    ),
    "purchase_orders": (
        _f("po_no", "PO number", True, "PO#", "PO Number", "PO No", "Purchase Order Number", "Order Number",
           "Order No"),
        _f("vendor", "vendor name", True, "Vendor Name", "Vendor", "Supplier", "Supplier Name", "Party Name"),
        _f("po_date", "PO date", False, "PO Date", "Order Date", "Date"),
        _f("item", "item", False, "Item Name", "Item", "Description", "Particulars"),
        _f("qty", "quantity", True, "Quantity", "Qty", "Qty Ordered", "Ordered Qty"),
        _f("rate", "rate", True, "Unit Price", "Rate", "Price"),
        _f("amount", "amount", False, "Sub Total", "Amount", "PO Amount", "Taxable Value"),
        _f("gst_rate", "GST rate", True, "Tax %", "GST Rate", "GST %", "Tax Rate"),
        _f("vendor_gstin", "vendor GSTIN", False, "Vendor GSTIN", "GSTIN", "GSTIN/UIN"),
    ),
    "payments": (
        _f("payment_id", "payment ID", True, "Payment Voucher", "Payment No", "Payment ID", "Voucher No", "Txn ID"),
        _f("payment_date", "payment date", False, "Txn Date", "Payment Date", "Value Date", "Date"),
        _f("payee", "payee", True, "Paid To", "Payee", "Beneficiary", "Vendor", "Party Name"),
        _f("invoice_ref", "invoice reference", True, "Against Bill", "Bill Ref", "Invoice Ref", "Inv Ref",
           "Invoice No", "Bill No"),
        _f("amount_paid", "amount paid", True, "Amount Paid", "Paid Amount", "Amount", "Debit"),
        _f("tds", "TDS", False, "TDS Deducted", "TDS", "TDS Amount"),
        _f("mode", "payment mode", False, "Mode", "Payment Mode"),
        _f("utr", "UTR", False, "UTR No", "UTR", "Bank Ref"),
    ),
}

# The field that identifies a row. A row without it is a footer or junk.
KEY_FIELDS = {"invoices": "invoice_no", "purchase_orders": "po_no", "payments": "payment_id"}


@dataclass(frozen=True)
class Table:
    """Rows are (physical line number, {original header: cell}). field_map is field name -> original header."""
    kind: str
    filename: str
    header: list[str]
    field_map: dict[str, str]
    rows: list[tuple[int, dict[str, str]]]
    warnings: list[str] = field(default_factory=list)


def load_table(data: bytes, kind: str, filename: str) -> Table:
    """Decode, find the header, map the columns and collect the data rows. Raises InputError."""
    if kind not in SCHEMAS:
        raise ValueError(f"unknown table kind: {kind}")
    text = _decode(data, filename)
    grid, header_at, field_map = _find_table(text, kind, filename)
    header = [cell.strip() for cell in grid[header_at][1]]
    rows, warnings = _data_rows(grid[header_at + 1:], header, field_map, kind, filename)
    if not rows:
        raise InputError(f"{filename} has a header but no data rows. Check the export covers the period and "
                         "upload it again.")
    return Table(kind, filename, header, field_map, rows, warnings)


def _decode(data: bytes, filename: str) -> str:
    if not data.strip():
        raise InputError(f"{filename} is empty. Export it again and upload the new file.")
    if data.startswith(b"PK\x03\x04"):
        raise InputError(f"{filename} looks like an Excel file, not a CSV. {EXCEL_HINT}")
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        text = "\x00"  # same message as any other binary junk
    if "\x00" in text:
        raise InputError(f"{filename} is not UTF-8 text. {EXCEL_HINT}")
    text = text.lstrip("\ufeff")  # a second BOM, which some exporters write after the first
    if not text.strip():
        raise InputError(f"{filename} is empty. Export it again and upload the new file.")
    return text


def _norm(text: str) -> str:
    """Lowercase and drop everything but letters and digits, so 'Inv. No' matches 'inv no'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _read_grid(text: str, delimiter: str) -> list[tuple[int, list[str]]]:
    """Every non-blank row as (physical line number, cells)."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [(reader.line_num, cells) for cells in reader if any(c.strip() for c in cells)]


def _delimiters(text: str) -> list[str]:
    """The sniffed delimiter first, then the other usual ones as a fallback."""
    try:
        sniffed = csv.Sniffer().sniff("\n".join(text.splitlines()[:MAX_HEADER_LINE]), delimiters=",;\t").delimiter
    except csv.Error:
        sniffed = ","
    return [sniffed] + [d for d in (",", ";", "\t") if d != sniffed]


def _map_columns(cells: list[str], fields: tuple[Field, ...]) -> dict[str, str]:
    """Map field name -> original header text. Each header column is claimed by at most one field."""
    by_norm: dict[str, str] = {}
    for cell in cells:
        by_norm.setdefault(_norm(cell), cell.strip())
    mapped: dict[str, str] = {}
    for fld in fields:
        for alias in fld.aliases:
            header = by_norm.get(_norm(alias))
            if header and header not in mapped.values():
                mapped[fld.name] = header
                break
    return mapped


def _required_hits(field_map: dict[str, str], fields: tuple[Field, ...]) -> int:
    return sum(1 for fld in fields if fld.required and fld.name in field_map)


def _find_table(text: str, kind: str, filename: str):
    """Return (grid, index of the header row, field_map) or raise InputError naming the missing column.

    Only non-blank lines count towards the MAX_HEADER_LINE window, because real exports have blank preamble lines.
    """
    fields = SCHEMAS[kind]
    best = None  # ((columns mapped, filled cells), grid, index, field_map): the likeliest header, for the error
    for delimiter in _delimiters(text):
        grid = _read_grid(text, delimiter)
        for index, (_, cells) in enumerate(grid[:MAX_HEADER_LINE]):
            field_map = _map_columns(cells, fields)
            if _required_hits(field_map, fields) >= MIN_HEADER_MATCHES:
                _require_columns(field_map, cells, fields, filename)
                return grid, index, field_map
            score = (len(field_map), sum(1 for c in cells if c.strip()))
            if best is None or score > best[0]:
                best = (score, grid, index, field_map)
    if best is None:
        raise InputError(f"{filename} has no header row we recognise in its first {MAX_HEADER_LINE} lines. "
                         "Check it is the right export.")
    _, grid, index, field_map = best
    _require_columns(field_map, grid[index][1], fields, filename)
    raise InputError(f"{filename} has no header row we recognise in its first {MAX_HEADER_LINE} lines.")


def _require_columns(field_map: dict[str, str], cells: list[str], fields: tuple[Field, ...], filename: str) -> None:
    for fld in fields:
        if fld.required and fld.name not in field_map:
            found = ", ".join(c.strip() for c in cells if c.strip())
            raise InputError(
                f"{filename} is missing a column for {fld.label}. "
                f"We looked for: {', '.join(fld.aliases)}; found: {found}. Rename a header or add the column."
            )


_TOTAL_LABEL = re.compile(r"^\s*(grand\s+)?total\b", re.IGNORECASE)


def _is_total_row(raw: dict[str, str], key: str) -> bool:
    """A footer: its key cell says 'Total', or the key is blank and some other cell says total."""
    return bool(_TOTAL_LABEL.match(key)) if key else any("total" in v.lower() for v in raw.values())


def _data_rows(grid, header: list[str], field_map: dict[str, str], kind: str, filename: str):
    """Turn raw cell lists into dicts, dropping footers and keyless rows (the latter with a warning)."""
    key_header = field_map[KEY_FIELDS[kind]]
    key_label = next(f.label for f in SCHEMAS[kind] if f.name == KEY_FIELDS[kind])
    parsed = []
    for line, cells in grid:
        padded = [c.strip() for c in cells] + [""] * (len(header) - len(cells))
        raw = {name: value for name, value in zip(header, padded, strict=False) if name}
        parsed.append((line, raw, raw.get(key_header, ""), any("\n" in c for c in cells)))
    # Total rows after the last real row are the footer. A total row with data after it is worth a warning.
    last_data = max((i for i, (_, raw, key, _) in enumerate(parsed) if not _is_total_row(raw, key)), default=-1)
    rows: list[tuple[int, dict[str, str]]] = []
    warnings: list[str] = []
    for index, (line, raw, key, multiline) in enumerate(parsed):
        if multiline:
            warnings.append(f"{filename} row {line} has a cell split over several lines, so row numbers after "
                            "it may be off.")
        if _is_total_row(raw, key):
            if index < last_data:
                warnings.append(f"{filename} row {line} looks like a total row in the middle of the data, "
                                "so it was skipped.")
        elif not key:
            warnings.append(f"{filename} row {line} has no {key_label}, so it was skipped.")
        else:
            rows.append((line, raw))
    return rows, warnings
