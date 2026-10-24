# Exception triage for accounts payable

Drop in three exports from Tally or Zoho Books (invoices, purchase orders, payments) and get every mismatch listed with the rupees at risk, the exact rows involved and one plain English sentence on what went wrong. Built for AP teams that do this by hand today, and as a quick check of a new customer's exports before they are loaded into an AP platform.

## Run it in 60 seconds
```bash
git clone https://github.com/arch-vvc/lets-fucking-get-a-fde-job-and-make-money.git
cd lets-fucking-get-a-fde-job-and-make-money
python demos/exception-triage/run.py --check
python demos/exception-triage/run.py
```
Then open http://127.0.0.1:8765 and click "Use sample data".

Needs Python 3.10 or newer. Nothing to install to run it, because the demo uses only the standard library. `pip install -r requirements.txt` is only for the tests.

`--check` takes about a second and prints a summary like this. The last line compares the result with the answer key for the planted errors in the sample data:
```
invoices checked: 40
exceptions found: 16
amount at risk: ₹7,11,230
explanations cached: 16/16
answer key: 16/16 matched, 0 extra
```
`explanations cached` is 16/16 because the Haiku sentences for the sample data are committed in `sample_data/explanations_cache.json`. Delete that file and the count drops to 0/16; the dashboard then falls back to the built in templates (see Explanations).

If port 8765 is busy, pass `--port 8800` (or any free port).

## What you will see
- A strip of totals: invoices checked, exceptions found, total amount at risk and how many are high severity.
- A table of exceptions, biggest rupee risk first, with type, severity, invoice, vendor, PO, amount and the suggested next step.
- Click a row to open the exact rows behind it, exactly as they appear in your invoice, PO and payment files, with the row numbers Excel shows. The cells that prove the finding are highlighted, and the amount columns come first so they are never off screen. For a duplicate you see the original invoice first, then the duplicate, with the original's PO and payments.
- A "Download CSV report" button with the same table, explanations included, ready to send to a vendor or a controller.

You can also drop your own three CSV files on the page instead of using the sample. They stay in memory on your machine and are gone when you stop the server.

## The eight exception types
| Type | What it means | Suggested action |
|---|---|---|
| Duplicate invoice | Same vendor, same invoice number once punctuation is ignored, same amount. The later bill is flagged. | Hold payment on the duplicate and confirm with vendor |
| Missing PO | No PO reference and no single PO to match by vendor, or a reference that is not in the PO list. | Get a PO raised or approval from the budget owner |
| Vendor name mismatch | The invoice and its PO name the vendor differently, such as "Shree Ganesh Steels" and "Sri Ganesh Steel". Nothing is at risk yet. | Merge the vendor master entries after checking GSTIN |
| Amount mismatch | Invoice rate differs from the PO rate by more than the tolerance. Risk is the difference plus GST. | Ask vendor for a credit note for the price difference |
| Quantity mismatch | Total billed against a PO is more than the quantity ordered. Billing less than ordered is fine. | Short-pay to the PO quantity and check the goods receipt note |
| GST error | Missing or malformed GSTIN, failed GSTIN check digit, a rate that is not a GST slab, a rate different from the PO, or a tax amount that does not add up. | Get a corrected tax invoice before claiming ITC |
| Partial payment | Paid plus TDS is less than the invoice total. | Confirm the reason for short payment or schedule the balance |
| Overpayment | Paid plus TDS is more than the invoice total. | Raise a refund request or adjust against the next bill |

Severity depends on the type and the amount. For example a missing PO is high from ₹1,00,000, an amount or quantity mismatch is high from ₹10,000, and a partial payment is medium from ₹50,000. The cutoffs are constants at the top of `triage/engine.py`.

An invoice with no payment yet is not an exception, because the payment file is usually an extract for one period.

## How matching works
1. Every file is read with flexible column names ("Bill Number", "Inv No" and "Invoice No" all work), skips Tally style heading lines above the header and skips "Grand Total" footers.
2. Values are normalised: `₹1,18,000`, `Rs. 45,000`, `(1,000.00)` and `45,000.00 Dr` are all amounts, dates are day-first in nine formats plus Excel serial numbers, invoice numbers like `INV-0042` and `inv/42` become the same key, and vendor names ignore "M/s", "Pvt Ltd", "Private Limited" and punctuation.
3. Each invoice is linked to its PO by the PO reference. If the reference is blank and the vendor has exactly one PO, that one is used. A reference that does not exist is never guessed at, because it is a finding in itself.
4. Each payment is linked to an invoice by its "Against Bill" reference, or by vendor and amount when that is blank and only one invoice fits.
5. The eight checks run on every linked invoice.

Tolerances default to: amounts may differ by the larger of ₹1 or 1%, quantities must match exactly, and payments must match within ₹1. To change them, edit the defaults of `Tolerance` in `triage/engine.py`, or pass your own, for example `triage_dir(path, Tolerance(amount_pct=0.02))`.

## Explanations
Each exception gets one sentence, in this order:
1. A cached sentence from a model, if one exists in `sample_data/explanations_cache.json` and it still matches the exception's facts.
2. Otherwise an offline template filled in from the same facts. It needs no network and cannot invent a number.

The dashboard only ever uses those two. It never calls a model, even for files you upload.

To write the cached sentences with Claude Haiku, run one of these once:
```bash
python demos/exception-triage/run.py --refresh --explainer claude-cli   # uses your Claude Code login
python demos/exception-triage/run.py --refresh --explainer anthropic    # needs ANTHROPIC_API_KEY
```
Each sentence is only accepted if it is one line, mentions the invoice number and uses no rupee amount that is not in the facts. Anything else falls back to the template.

## The sample data
Everything in `sample_data/` is synthetic. The vendors and GSTINs are fictional. The files are shaped like real exports: a Tally purchase register with heading lines and a Grand Total footer, a Zoho style PO list, and a payment voucher list, with mixed date and amount formats, blank cells and Indian number grouping.

There are 40 invoices, 30 purchase orders and 35 payments, with 16 exceptions planted: 2 amount mismatches, 2 quantity mismatches, 2 duplicates, 2 missing POs, 2 partial payments, 1 overpayment, 3 GST errors and 2 vendor name mismatches. They are mixed in with look-alikes that must not be flagged, such as a vendor written as `M/s. Kumar Packaging Pvt. Ltd.` on one document and `KUMAR PACKAGING PRIVATE LIMITED` on another, payments with TDS that settle in full, and invoices paid in two instalments.

`sample_data/expected_exceptions.csv` is the answer key. It is written by hand in `sample_data/generate.py` and never taken from the engine's output, so `--check` and the tests compare the engine against an independent source.

## Tests
```bash
pip install -r requirements.txt
python -m pytest -q demos/exception-triage/tests
```
No test touches the network or starts a model.

## Limits
- One line item per invoice and per PO. Real multi line invoices are out of scope.
- Dates are read day-first, so `03/04/2024` is 3 April.
- CSV only, not xlsx. In Excel use Save As, CSV UTF-8. UTF-8 and UTF-16 files are accepted, other encodings get a clear error.
- Files over 5 MB are refused.
- Single user, local only. There is no login.
- The three way match is PO, invoice and payment. There is no goods receipt file, so quantity is checked against the PO only.
- Credit notes (negative invoices) are skipped with a warning, not netted.
- The amount at risk adds up findings per invoice. If one invoice has two findings, the same rupees can count twice in the headline.
- Not covered yet: GSTR-2B and ITC eligibility, place of supply (IGST against CGST plus SGST), TDS section and rate checks, MSME payment deadlines, vendor master and bank detail changes.
- The accuracy figure (16 of 16) is measured against a planted answer key on synthetic data. It shows the checks work, not how they score on a real export.
