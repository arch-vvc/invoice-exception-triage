"""Three-way match of invoices, purchase orders and payments, and the eight exception checks (C3, C4, C5)."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import NamedTuple

from .loader import InputError, load_table
from .normalise import (
    Invoice,
    Payment,
    PurchaseOrder,
    format_inr,
    gstin_problem,
    is_gst_slab,
    to_invoices,
    to_payments,
    to_purchase_orders,
)

SAMPLE_FILES = {"invoices": "invoices.csv", "purchase_orders": "purchase_orders.csv", "payments": "payments.csv"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
ACTIONS = {
    "duplicate_invoice": "Hold payment on the duplicate and confirm with vendor",
    "missing_po": "Get a PO raised or approval from the budget owner",
    "vendor_name_mismatch": "Merge the vendor master entries after checking GSTIN",
    "amount_mismatch": "Ask vendor for a credit note for the price difference",
    "quantity_mismatch": "Short-pay to the PO quantity and check the goods receipt note",
    "gst_error": "Get a corrected tax invoice before claiming ITC",
    "partial_payment": "Confirm the reason for short payment or schedule the balance",
    "overpayment": "Raise a refund request or adjust against the next bill",
}
# Risk (in rupees) at or above which a type moves up a severity level.
MISSING_PO_HIGH = 100_000
AMOUNT_OR_QTY_HIGH = 10_000
PARTIAL_PAYMENT_MEDIUM = 50_000


@dataclass(frozen=True)
class Tolerance:
    """An amount check passes when |diff| <= max(amount_abs, amount_pct * reference)."""
    amount_pct: float = 0.01
    amount_abs: float = 1.0
    qty_abs: float = 0.0
    payment_abs: float = 1.0

    def amount_ok(self, diff: float, reference: float) -> bool:
        return abs(round(diff, 2)) <= max(self.amount_abs, self.amount_pct * abs(reference))


@dataclass
class TriageException:
    id: str
    type: str
    severity: str
    invoice_ids: list[str]
    po_ids: list[str]
    payment_ids: list[str]
    source_rows: dict[str, list[int]]
    amount_at_risk: float
    suggested_action: str
    facts: dict[str, str | int | float | bool]
    # The cells that prove the finding: {"kind": "invoices", "row": 17, "field": "total"}. The server turns the
    # field names into the user's own column headers.
    flagged_cells: list[dict[str, str | int]] = field(default_factory=list)


@dataclass
class TriageResult:
    invoices: list[Invoice]
    purchase_orders: list[PurchaseOrder]
    payments: list[Payment]
    exceptions: list[TriageException]
    warnings: list[str] = field(default_factory=list)
    unmatched_payment_ids: list[str] = field(default_factory=list)
    invoices_checked: int = 0
    amount_at_risk: float = 0.0
    field_headers: dict[str, dict[str, str]] = field(default_factory=dict)  # kind -> {field name: user's header}


# ---- building exceptions ----

def _g(gst_rate: float | None) -> float:
    return (gst_rate or 0.0) / 100


def _money(**values: float) -> dict[str, float | str]:
    """Each amount plus a `<name>_display` string, so the dashboard never formats money itself."""
    facts: dict[str, float | str] = {}
    for name, value in values.items():
        facts[name] = round(value, 2)
        facts[f"{name}_display"] = format_inr(value)
    return facts


def _tier(risk: float, cutoff: float, above: str, below: str) -> str:
    return above if risk >= cutoff else below


def _cells(kind: str, rows: list[int], *fields: str) -> list[dict[str, str | int]]:
    """Flag these fields on each of these source rows."""
    return [{"kind": kind, "row": row, "field": name} for row in rows for name in fields]


def _make(kind: str, severity: str, invoice: Invoice, risk: float, extra: dict, po: PurchaseOrder | None = None,
          more_invoices: tuple[Invoice, ...] | list[Invoice] = (), payments: list[Payment] | None = None,
          flag: list[dict[str, str | int]] | None = None):
    """Assemble a TriageException. The primary invoice always comes first in the id and row lists."""
    invoices = [invoice, *more_invoices]
    payments = payments or []
    facts = {"invoice_no": invoice.invoice_no, "vendor": invoice.vendor, "po_no": po.po_no if po else ""}
    return TriageException(
        id="", type=kind, severity=severity,
        invoice_ids=[i.invoice_no for i in invoices],
        po_ids=[po.po_no] if po else [],
        payment_ids=[p.payment_id for p in payments],
        source_rows={
            "invoices": [i.source_row for i in invoices],
            "purchase_orders": [po.source_row] if po else [],
            "payments": [p.source_row for p in payments],
        },
        amount_at_risk=round(risk, 2), suggested_action=ACTIONS[kind], facts=facts | extra,
        flagged_cells=flag or [],
    )


# ---- linking (C3) ----

def _find_duplicates(invoices: list[Invoice], tol: Tolerance) -> dict[Invoice, Invoice]:
    """Map each later duplicate to the first invoice it repeats (same vendor, number and total)."""
    originals: dict[tuple[str, str], list[Invoice]] = defaultdict(list)
    duplicates: dict[Invoice, Invoice] = {}
    for inv in sorted(invoices, key=lambda i: i.source_row):
        seen = originals[(inv.vendor_key, inv.invoice_key)]
        match = next((o for o in seen if _same_total(inv, o, tol)), None)
        if match:
            duplicates[inv] = match
        else:
            seen.append(inv)
    return duplicates


def _same_total(a: Invoice, b: Invoice, tol: Tolerance) -> bool:
    return a.total is not None and b.total is not None and tol.amount_ok(a.total - b.total, b.total)


def _link_pos(invoices: list[Invoice], pos: list[PurchaseOrder]) -> dict[Invoice, PurchaseOrder]:
    """Link by PO reference. Only a blank reference falls back to the vendor, and only if it has one PO."""
    by_key: dict[str, PurchaseOrder] = {}
    by_vendor: dict[str, list[PurchaseOrder]] = defaultdict(list)
    for po in pos:
        by_key.setdefault(po.po_key, po)
        by_vendor[po.vendor_key].append(po)
    linked = {}
    for inv in invoices:
        if inv.po_key:
            po = by_key.get(inv.po_key)  # a wrong reference stays unlinked: that is a finding
        else:
            same_vendor = by_vendor.get(inv.vendor_key, [])
            po = same_vendor[0] if len(same_vendor) == 1 else None
        if po:
            linked[inv] = po
    return linked


# Words too common in Indian business names to say two vendors are the same.
GENERIC_NAME_WORDS = {
    "AND", "TRADERS", "TRADER", "TRADING", "TRADE", "ENTERPRISES", "ENTERPRISE", "INDUSTRIES", "INDUSTRY", "SONS",
    "BROTHERS", "BROS", "WORKS", "SERVICES", "SUPPLIERS", "SUPPLIES", "SALES", "AGENCIES", "AGENCY", "CORPORATION",
    "CORP", "GROUP", "INDIA",
}


def _link_payments(payments: list[Payment], invoices: list[Invoice], tol: Tolerance):
    """Return ({invoice: its payments}, [(unmatched payment, reason)])."""
    by_key: dict[str, list[Invoice]] = defaultdict(list)
    for inv in sorted(invoices, key=lambda i: i.source_row):
        by_key[inv.invoice_key].append(inv)
    paid_by: dict[Invoice, list[Payment]] = defaultdict(list)
    unmatched = []
    for pay in payments:
        invoice, reason = _match_payment(pay, by_key, invoices, tol)
        if invoice:
            paid_by[invoice].append(pay)
        else:
            unmatched.append((pay, reason))
    return paid_by, unmatched


def _name_overlap(a: str, b: str) -> int:
    """How many distinctive words two vendor keys share."""
    return len((set(a.split()) & set(b.split())) - GENERIC_NAME_WORDS)


def _match_payment(pay: Payment, by_key: dict[str, list[Invoice]], invoices: list[Invoice], tol: Tolerance):
    """Return (invoice or None, reason it could not be matched)."""
    unmatched = f"Payment {pay.payment_id} (payments row {pay.source_row}) could not be matched to an invoice."
    if pay.invoice_key:
        candidates = by_key.get(pay.invoice_key, [])
        if len(candidates) <= 1:
            return (candidates[0] if candidates else None), unmatched
        # Several vendors reuse this bill number: take the payee's own, else the one name that fits best.
        exact = next((i for i in candidates if i.vendor_key == pay.payee_key), None)
        if exact:
            return exact, ""
        scores = [_name_overlap(i.vendor_key, pay.payee_key) for i in candidates]
        if max(scores) > 0 and scores.count(max(scores)) == 1:
            return candidates[scores.index(max(scores))], ""
        return None, (f"Payment {pay.payment_id} (payments row {pay.source_row}) was not matched: bill number "
                      f"{candidates[0].invoice_no} belongs to several vendors and none clearly matches "
                      f"'{pay.payee}'.")
    fits = [i for i in invoices if i.vendor_key == pay.payee_key and i.total is not None
            and tol.amount_ok(i.total - pay.settled, i.total)]
    return (fits[0] if len(fits) == 1 else None), unmatched


class Overrun(NamedTuple):
    cumulative: float
    excess: float
    earlier: list[Invoice]


def _quantity_overruns(po_for: dict[Invoice, PurchaseOrder], tol: Tolerance) -> dict[Invoice, Overrun]:
    """Invoices that push the quantity billed against a PO past what was ordered."""
    by_po: dict[PurchaseOrder, list[Invoice]] = defaultdict(list)
    for inv, po in po_for.items():
        by_po[po].append(inv)
    overruns = {}
    for po, invoices in by_po.items():
        if po.qty is None:
            continue
        invoices.sort(key=lambda i: (i.invoice_date or date.max, i.source_row))
        running = 0.0
        for index, inv in enumerate(invoices):
            qty = inv.qty or 0.0
            running = round(running + qty, 4)
            if running > po.qty + tol.qty_abs:
                overruns[inv] = Overrun(running, min(qty, running - po.qty), invoices[:index])
    return overruns


# ---- the checks (C5). Each returns an exception or None. ----

def _duplicate(dup: Invoice, original: Invoice, po: PurchaseOrder | None,
               payments: list[Payment]) -> TriageException:
    """The duplicate carries the original's PO and payments, so the reader can see what was already paid."""
    total = dup.total or 0.0
    extra = {"original_invoice_no": original.invoice_no} | _money(total=total)
    flag = _cells("invoices", [dup.source_row, original.source_row], "total")
    return _make("duplicate_invoice", "high", dup, total, extra, po=po, more_invoices=[original],
                 payments=payments, flag=flag)


def _missing_po(inv: Invoice) -> TriageException:
    risk = inv.total or 0.0
    return _make("missing_po", _tier(risk, MISSING_PO_HIGH, "high", "medium"), inv, risk, {"po_ref_given": inv.po_ref},
                 flag=_cells("invoices", [inv.source_row], "po_ref"))


def _vendor_mismatch(inv: Invoice, po: PurchaseOrder) -> TriageException | None:
    if inv.vendor_key == po.vendor_key:
        return None
    extra = {"invoice_vendor": inv.vendor, "po_vendor": po.vendor}
    flag = _cells("invoices", [inv.source_row], "vendor") + _cells("purchase_orders", [po.source_row], "vendor")
    return _make("vendor_name_mismatch", "low", inv, 0.0, extra, po=po, flag=flag)


def _amount_mismatch(inv: Invoice, po: PurchaseOrder, tol: Tolerance) -> TriageException | None:
    if inv.qty is None or po.rate is None or inv.taxable_value is None:
        return None
    expected = inv.qty * po.rate
    diff = round(inv.taxable_value - expected, 2)
    if tol.amount_ok(diff, expected):
        return None
    risk = abs(diff) * (1 + _g(inv.gst_rate))
    rate = inv.rate if inv.rate is not None else (inv.taxable_value / inv.qty if inv.qty else 0.0)
    extra = _money(invoice_rate=rate, po_rate=po.rate) | {"qty": inv.qty} | _money(diff=diff)
    extra["gst_rate"] = inv.gst_rate or 0.0  # the risk is the price difference plus this much GST
    proof = "rate" if inv.rate is not None else "taxable_value"
    flag = _cells("invoices", [inv.source_row], proof) + _cells("purchase_orders", [po.source_row], "rate")
    return _make("amount_mismatch", _tier(risk, AMOUNT_OR_QTY_HIGH, "high", "medium"), inv, risk, extra, po=po,
                 flag=flag)


def _quantity_mismatch(inv: Invoice, po: PurchaseOrder, overrun: Overrun | None) -> TriageException | None:
    if overrun is None or po.rate is None:
        return None
    risk = overrun.excess * po.rate * (1 + _g(po.gst_rate))
    extra = {"invoiced_qty": overrun.cumulative, "po_qty": po.qty, "excess_qty": overrun.excess}
    rows = [inv.source_row, *(i.source_row for i in overrun.earlier)]
    flag = _cells("invoices", rows, "qty") + _cells("purchase_orders", [po.source_row], "qty")
    return _make("quantity_mismatch", _tier(risk, AMOUNT_OR_QTY_HIGH, "high", "medium"), inv, risk, extra,
                 po=po, more_invoices=overrun.earlier, flag=flag)


def _gst_finding(inv: Invoice, po: PurchaseOrder | None, tol: Tolerance) -> tuple[str, float] | None:
    """The first GST problem as (reason, rupees at risk), checked in the order of contract C5."""
    g = _g(inv.gst_rate)
    taxable = inv.taxable_value or 0.0
    computed = taxable * g
    tax = inv.gst_amount if inv.gst_amount is not None else computed
    problem = gstin_problem(inv.vendor_gstin)
    if problem == "missing" and g > 0:
        return "missing", tax
    if problem in ("format", "checksum"):
        return problem, tax
    if inv.gst_rate is not None and not is_gst_slab(inv.gst_rate):
        return "slab", tax
    if po is not None and po.gst_rate is not None and inv.gst_rate is not None and po.gst_rate != inv.gst_rate:
        return "rate_vs_po", abs(taxable * (g - _g(po.gst_rate)))
    if inv.taxable_value is not None and inv.gst_amount is not None \
            and not tol.amount_ok(inv.gst_amount - computed, computed):
        return "amount", abs(inv.gst_amount - computed)
    return None


def _gst_error(inv: Invoice, po: PurchaseOrder | None, tol: Tolerance) -> TriageException | None:
    finding = _gst_finding(inv, po, tol)
    if finding is None:
        return None
    reason, risk = finding
    extra = {
        "reason": reason, "gstin": inv.vendor_gstin,
        "gst_rate": inv.gst_rate if inv.gst_rate is not None else "",
        "po_gst_rate": po.gst_rate if po and po.gst_rate is not None else "",
    }
    severity = "high" if reason in ("missing", "format", "checksum") else "medium"
    return _make("gst_error", severity, inv, risk, extra, po=po, flag=_gst_flags(reason, inv, po))


# Which fields prove each GST reason: (invoice fields, PO fields).
GST_PROOF = {
    "missing": (("vendor_gstin", "gst_rate"), ()),
    "format": (("vendor_gstin",), ()),
    "checksum": (("vendor_gstin",), ()),
    "slab": (("gst_rate",), ()),
    "rate_vs_po": (("gst_rate",), ("gst_rate",)),
    "amount": (("gst_rate", "gst_amount"), ()),
}


def _gst_flags(reason: str, inv: Invoice, po: PurchaseOrder | None) -> list[dict[str, str | int]]:
    invoice_fields, po_fields = GST_PROOF[reason]
    flag = _cells("invoices", [inv.source_row], *invoice_fields)
    return flag + (_cells("purchase_orders", [po.source_row], *po_fields) if po else [])


def _payment_exception(inv: Invoice, po: PurchaseOrder | None, payments: list[Payment],
                       tol: Tolerance) -> TriageException | None:
    if inv.total is None or not payments:
        return None
    paid = sum(p.amount_paid for p in payments)
    tds = sum(p.tds for p in payments)
    settled = round(paid + tds, 2)
    if 0 < settled < inv.total - tol.payment_abs:
        outstanding = inv.total - settled
        extra = _money(total=inv.total, paid=paid, tds=tds, outstanding=outstanding)
        severity = _tier(outstanding, PARTIAL_PAYMENT_MEDIUM, "medium", "low")
        return _make("partial_payment", severity, inv, outstanding, extra, po=po, payments=payments,
                     flag=_payment_flags(inv, payments))
    if settled > inv.total + tol.payment_abs:
        excess = settled - inv.total
        extra = _money(total=inv.total, paid=paid, tds=tds, excess=excess)
        if abs(settled - 2 * inv.total) <= tol.payment_abs:
            extra["paid_twice"] = True
        return _make("overpayment", "high", inv, excess, extra, po=po, payments=payments,
                     flag=_payment_flags(inv, payments))
    return None


def _payment_flags(inv: Invoice, payments: list[Payment]) -> list[dict[str, str | int]]:
    """The invoice total, and what was paid (plus the TDS on any payment that had some)."""
    flag = _cells("invoices", [inv.source_row], "total") + _cells("payments", [p.source_row for p in payments],
                                                                 "amount_paid")
    return flag + _cells("payments", [p.source_row for p in payments if p.tds], "tds")


def _invoice_exceptions(inv: Invoice, po: PurchaseOrder | None, overrun: Overrun | None,
                        payments: list[Payment], tol: Tolerance) -> list[TriageException]:
    if po is None:
        found = [_missing_po(inv)]
    else:
        found = [_vendor_mismatch(inv, po), _amount_mismatch(inv, po, tol), _quantity_mismatch(inv, po, overrun)]
    found += [_gst_error(inv, po, tol), _payment_exception(inv, po, payments, tol)]
    return [exc for exc in found if exc]


def _is_credit_note(inv: Invoice) -> bool:
    return any(value is not None and value < 0 for value in (inv.total, inv.taxable_value, inv.qty))


def _repeated_po_warnings(pos: list[PurchaseOrder]) -> list[str]:
    seen: set[str] = set()
    warnings = []
    for po in sorted(pos, key=lambda p: p.source_row):
        if po.po_key and po.po_key in seen:
            warnings.append(f"{po.po_no} appears more than once; the first row is used.")
        seen.add(po.po_key)
    return warnings


# ---- entry points ----

def triage(invoices: list[Invoice], pos: list[PurchaseOrder], payments: list[Payment],
           tol: Tolerance | None = None) -> TriageResult:
    """Link the three lists, run every check, and return the exceptions sorted by rupees at risk."""
    tol = tol or Tolerance()
    credit_notes = [inv for inv in invoices if _is_credit_note(inv)]
    checked = [inv for inv in invoices if inv not in credit_notes]
    duplicates = _find_duplicates(checked, tol)
    live = [inv for inv in checked if inv not in duplicates]
    po_for = _link_pos(live, pos)
    overruns = _quantity_overruns(po_for, tol)
    paid_by, unmatched = _link_payments(payments, live, tol)

    found = [_duplicate(dup, original, po_for.get(original), paid_by.get(original, []))
             for dup, original in duplicates.items()]
    for inv in live:
        found += _invoice_exceptions(inv, po_for.get(inv), overruns.get(inv), paid_by.get(inv, []), tol)
    found.sort(key=lambda e: (-e.amount_at_risk, SEVERITY_RANK[e.severity], e.invoice_ids[0], e.type))
    for number, exc in enumerate(found, start=1):
        exc.id = f"EX-{number:02d}"

    return TriageResult(
        invoices=invoices, purchase_orders=pos, payments=payments, exceptions=found,
        warnings=[
            *(f"Row {inv.source_row} ({inv.invoice_no}) looks like a credit note and was not matched. "
              "Credit notes are not supported yet." for inv in credit_notes),
            *_repeated_po_warnings(pos),
            *(reason for _, reason in unmatched),
        ],
        unmatched_payment_ids=[p.payment_id for p, _ in unmatched],
        invoices_checked=len(checked),
        amount_at_risk=round(sum(e.amount_at_risk for e in found), 2),
    )


def triage_bytes(files: dict[str, tuple[str, bytes]], tol: Tolerance | None = None) -> TriageResult:
    """Run triage on {kind: (filename, bytes)}. Raises InputError with a friendly message."""
    for kind, default_name in SAMPLE_FILES.items():
        if kind not in files:
            raise InputError(f"{default_name} was not provided. Upload it and try again.")
    tables = {kind: load_table(data, kind, name) for kind, (name, data) in files.items() if kind in SAMPLE_FILES}
    warnings = [w for table in tables.values() for w in table.warnings]
    invoices = to_invoices(tables["invoices"], warnings)
    pos = to_purchase_orders(tables["purchase_orders"], warnings)
    payments = to_payments(tables["payments"], warnings)
    result = triage(invoices, pos, payments, tol)
    result.warnings[:0] = warnings
    result.field_headers = {kind: dict(table.field_map) for kind, table in tables.items()}
    return result


def triage_dir(path: str | Path, tol: Tolerance | None = None) -> TriageResult:
    """Run triage on invoices.csv, purchase_orders.csv and payments.csv in a folder."""
    files = {}
    for kind, name in SAMPLE_FILES.items():
        file = Path(path) / name
        if not file.is_file():
            raise InputError(f"{name} was not found. Put invoices.csv, purchase_orders.csv and payments.csv in "
                             "the same folder.")
        files[kind] = (name, file.read_bytes())
    return triage_bytes(files, tol)
