"""Turn messy cell text into clean values and typed records (contract C2 and C3)."""
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .loader import Table

Warnings = list[str] | None

GST_SLABS = (0.0, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0)
_BLANKS = {"", "-", "NA"}


def _warn(warnings: Warnings, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def _round2(value: str | float) -> float:
    """Round half up to 2 decimals (plain round() would give 12.34 for 12.345)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---- amounts, dates, quantities, rates ----

_AMOUNT = re.compile(r"^(?:₹|rs\.?|inr)?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(?:dr|cr)?$", re.IGNORECASE)


def parse_amount(text: str, warnings: Warnings = None) -> float | None:
    """'₹1,18,000', 'Rs. 45,000', '45,000.00 Dr' and '(1,000.00)' -> float. Blank, '-' and 'NA' -> None."""
    cleaned = text.strip()
    if cleaned.upper() in _BLANKS:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1].strip()
    match = _AMOUNT.match(cleaned)
    if not match:
        _warn(warnings, f"could not read amount '{text.strip()}'")
        return None
    value = _round2(match.group(1).replace(",", ""))
    return -value if negative else value


_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d-%b-%y",
    "%d %b %Y", "%d %B %Y", "%Y-%m-%d",
)
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_date(text: str, warnings: Warnings = None) -> date | None:
    """Day-first dates in the formats Indian exports use, or an Excel serial number."""
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.isdigit() and len(cleaned) == 5 and 40000 <= int(cleaned) <= 60000:
        return _EXCEL_EPOCH + timedelta(days=int(cleaned))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    _warn(warnings, f"could not read date '{cleaned}'")
    return None


_QTY = re.compile(r"^(\d[\d,]*(?:\.\d+)?)(?:\s*[A-Za-z][A-Za-z.\s]*)?$")


def parse_qty(text: str, warnings: Warnings = None) -> float | None:
    """The leading number, so '1,000 pcs' -> 1000.0. Trailing unit words are ignored."""
    cleaned = text.strip()
    if cleaned.upper() in _BLANKS:
        return None
    match = _QTY.match(cleaned)
    if not match:
        _warn(warnings, f"could not read quantity '{cleaned}'")
        return None
    return _round2(match.group(1).replace(",", ""))


_GST_RATE = re.compile(r"^(?:[A-Za-z]+\s*)?(\d+(?:\.\d+)?)\s*%?$")


def parse_gst_rate(text: str, warnings: Warnings = None) -> float | None:
    """'18', '18%', '18.00 %' and 'IGST 18%' all give 18.0 (always a percent)."""
    cleaned = text.strip()
    if cleaned.upper() in _BLANKS:
        return None
    match = _GST_RATE.match(cleaned)
    if not match:
        _warn(warnings, f"could not read GST rate '{cleaned}'")
        return None
    return float(match.group(1))


def is_gst_slab(rate: float) -> bool:
    return any(abs(rate - slab) < 1e-9 for slab in GST_SLABS)


# ---- keys for matching ----

def doc_key(text: str) -> str:
    """'INV-0042', 'inv/42' and 'INV 042' all give 'INV42'."""
    tokens = re.findall(r"[A-Z0-9]+", text.upper())
    return "".join(re.sub(r"\d+", lambda m: m.group().lstrip("0") or "0", token) for token in tokens)


_VENDOR_NOISE = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "CO", "COMPANY", "INC", "THE", "MS"}


def vendor_key(name: str) -> str:
    """'M/s. Kumar Packaging Pvt. Ltd.' and 'KUMAR PACKAGING PRIVATE LIMITED' both give 'KUMAR PACKAGING'."""
    upper = re.sub(r"^M\s*/\s*S\.?\s+", "", name.upper())
    words = re.sub(r"[^A-Z0-9]", " ", upper.replace("&", " AND ")).split()
    return " ".join(word for word in words if word not in _VENDOR_NOISE)


# ---- GSTIN ----

_GSTIN = re.compile(r"^(0[1-9]|[1-2][0-9]|3[0-8])[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def clean_gstin(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def gstin_check_char(first14: str) -> str:
    """The 15th GSTIN character, computed from the first 14."""
    total = 0
    for i, char in enumerate(first14):
        product = _GSTIN_CHARS.index(char) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_problem(text: str | None) -> str | None:
    """None when the GSTIN is fine, else 'missing', 'format' or 'checksum'."""
    gstin = clean_gstin(text)
    if not gstin:
        return "missing"
    if not _GSTIN.match(gstin):
        return "format"
    return None if gstin[14] == gstin_check_char(gstin[:14]) else "checksum"


# ---- display ----

def format_inr(amount: float) -> str:
    """Whole rupees with Indian grouping: 12345678.5 -> '₹1,23,45,679'."""
    rupees = math.floor(abs(amount) + 0.5)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    sign = "-" if amount < 0 and rupees else ""
    return f"{sign}₹{','.join(groups + [tail])}"


# ---- records ----
# eq=False keeps identity equality and hashing, so the engine can use records as dict keys.

@dataclass(frozen=True, eq=False)
class Invoice:
    invoice_no: str
    invoice_key: str
    vendor: str
    vendor_key: str
    vendor_gstin: str
    invoice_date: date | None
    po_ref: str
    po_key: str
    item: str | None
    qty: float | None
    rate: float | None
    taxable_value: float | None
    gst_rate: float | None
    gst_amount: float | None
    total: float | None
    source_row: int
    raw: dict[str, str]


@dataclass(frozen=True, eq=False)
class PurchaseOrder:
    po_no: str
    po_key: str
    vendor: str
    vendor_key: str
    vendor_gstin: str
    po_date: date | None
    item: str | None
    qty: float | None
    rate: float | None
    amount: float | None
    gst_rate: float | None
    source_row: int
    raw: dict[str, str]


@dataclass(frozen=True, eq=False)
class Payment:
    payment_id: str
    payment_date: date | None
    payee: str
    payee_key: str
    invoice_ref: str
    invoice_key: str
    amount_paid: float
    tds: float
    settled: float
    mode: str | None
    utr: str | None
    source_row: int
    raw: dict[str, str]


# ---- building records from a Table ----

def _text(text: str, warnings: Warnings = None) -> str:
    return text


class _Row:
    """Reads one raw row field by field, turning bad cells into warnings that name the file and row."""

    def __init__(self, table: Table, source_row: int, raw: dict[str, str], warnings: list[str]):
        self.table, self.source_row, self.raw, self.warnings = table, source_row, raw, warnings

    def text(self, name: str) -> str:
        header = self.table.field_map.get(name)
        return self.raw.get(header, "") if header else ""

    def warn(self, name: str, problem: str) -> None:
        header = self.table.field_map.get(name, name)
        self.warnings.append(f"{self.table.filename} row {self.source_row}, column '{header}': {problem}")

    def get(self, name: str, parser: Callable[..., Any] = _text, required: bool = False) -> Any:
        """Parse a field. Blank gives None (or "" for text), plus a warning when the cell was required."""
        value = self.text(name)
        problems: list[str] = []
        if value:
            result = parser(value, problems)
        else:
            result = "" if parser is _text else None
        if required and result in (None, "") and not problems:
            problems.append("value is missing")
        for problem in problems:
            self.warn(name, problem)
        return result


def _rows(table: Table, warnings: list[str]):
    return (_Row(table, line, raw, warnings) for line, raw in table.rows)


def to_invoices(table: Table, warnings: list[str]) -> list[Invoice]:
    """Build Invoice records. Problems with individual cells are appended to `warnings`."""
    invoices = []
    for row in _rows(table, warnings):
        qty = row.get("qty", parse_qty, required=True)
        rate = row.get("rate", parse_amount)
        taxable = row.get("taxable_value", parse_amount)
        if taxable is None and qty is not None and rate is not None:
            taxable = _round2(qty * rate)
        elif taxable is None:
            row.warn("taxable_value", "value is missing")
        po_ref = row.get("po_ref")
        vendor = row.get("vendor", required=True)
        invoices.append(Invoice(
            invoice_no=row.get("invoice_no"), invoice_key=doc_key(row.get("invoice_no")),
            vendor=vendor, vendor_key=vendor_key(vendor),
            vendor_gstin=clean_gstin(row.get("vendor_gstin")),
            invoice_date=row.get("invoice_date", parse_date, required=True),
            po_ref=po_ref, po_key=doc_key(po_ref),
            item=row.get("item") or None, qty=qty, rate=rate, taxable_value=taxable,
            gst_rate=row.get("gst_rate", parse_gst_rate, required=True),
            gst_amount=row.get("gst_amount", parse_amount),
            total=row.get("total", parse_amount, required=True),
            source_row=row.source_row, raw=row.raw,
        ))
    return invoices


def to_purchase_orders(table: Table, warnings: list[str]) -> list[PurchaseOrder]:
    """Build PurchaseOrder records. Problems with individual cells are appended to `warnings`."""
    orders = []
    for row in _rows(table, warnings):
        vendor = row.get("vendor", required=True)
        orders.append(PurchaseOrder(
            po_no=row.get("po_no"), po_key=doc_key(row.get("po_no")),
            vendor=vendor, vendor_key=vendor_key(vendor),
            vendor_gstin=clean_gstin(row.get("vendor_gstin")),
            po_date=row.get("po_date", parse_date),
            item=row.get("item") or None,
            qty=row.get("qty", parse_qty, required=True),
            rate=row.get("rate", parse_amount, required=True),
            amount=row.get("amount", parse_amount),
            gst_rate=row.get("gst_rate", parse_gst_rate, required=True),
            source_row=row.source_row, raw=row.raw,
        ))
    return orders


def to_payments(table: Table, warnings: list[str]) -> list[Payment]:
    """Build Payment records. Problems with individual cells are appended to `warnings`."""
    payments = []
    for row in _rows(table, warnings):
        payee = row.get("payee", required=True)
        invoice_ref = row.get("invoice_ref")
        paid = row.get("amount_paid", parse_amount, required=True) or 0.0
        tds = row.get("tds", parse_amount) or 0.0
        payments.append(Payment(
            payment_id=row.get("payment_id"), payment_date=row.get("payment_date", parse_date),
            payee=payee, payee_key=vendor_key(payee),
            invoice_ref=invoice_ref, invoice_key=doc_key(invoice_ref),
            amount_paid=paid, tds=tds, settled=_round2(paid + tds),
            mode=row.get("mode") or None, utr=row.get("utr") or None,
            source_row=row.source_row, raw=row.raw,
        ))
    return payments
