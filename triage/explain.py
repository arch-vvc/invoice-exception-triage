"""One plain English sentence per exception.

Resolution order for each exception: cache hit, then the chosen model backend, then an offline template.
A model sentence is only used (and cached) if it passes `is_valid_sentence`; the offline templates are
built from the engine's facts, so they can never invent a number.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .engine import TriageException
from .normalise import format_inr

Explainer = Callable[[list[TriageException]], dict[str, str]]
Runner = Callable[[str], str]

DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "sample_data" / "explanations_cache.json"
BACKENDS = ("offline", "claude-cli", "anthropic")
WORKERS = 4
CLI_TIMEOUT_S = 60
API_TIMEOUT_S = 30
API_URL = "https://api.anthropic.com/v1/messages"
API_MODEL = "claude-haiku-5-5"
MAX_SENTENCE_CHARS = 300
RUPEE_AMOUNT = re.compile(r"(?:₹|\bINR|\bRs\.?)\s*(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
RUPEES_WORD = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*rupees?\b", re.IGNORECASE)
RATE_FACTS = ("invoice_rate", "po_rate")
# US spellings a model tends to produce. Everything a user reads is in British English.
AMERICAN_SPELLING = re.compile(
    r"\b\w*(?:authoriz|organiz|recogniz|categoriz|prioritiz|analyz|summariz|minimiz|finaliz)\w*", re.IGNORECASE)


def warn(message: str) -> None:
    print(f"explain: {message}", file=sys.stderr)


# ---- number formatting ----

def _grouped(value: float, decimals: int) -> str:
    """Indian grouping: 1234567.5 -> '12,34,567.50' with 2 decimals, '12,34,568' with none."""
    whole, _, fraction = f"{abs(value):.{decimals}f}".partition(".")
    text = format_inr(int(whole))[1:] + (f".{fraction}" if fraction else "")
    return f"-{text}" if value < 0 else text


def _num(value) -> str:
    """Quantities and percents: 1600 -> '1,600', 12.5 -> '12.5', up to 2 decimals."""
    if not isinstance(value, (int, float)):
        return str(value)
    value = round(value, 2)
    text = _grouped(value, 0 if value == int(value) else 2)
    return text.rstrip("0") if "." in text else text


def _rupees(value: float) -> str:
    """Unit rates keep their paise (₹12.50) because format_inr rounds to whole rupees."""
    value = round(value, 2)
    return "₹" + _grouped(value, 0 if value == int(value) else 2)


def _display_facts(facts: dict) -> dict:
    """The facts with exact rate strings in place of the whole-rupee `*_display` values."""
    shown = dict(facts)
    for name in RATE_FACTS:
        if isinstance(facts.get(name), (int, float)):
            shown[f"{name}_display"] = _rupees(facts[name])
    return shown


def _abs_money(facts: dict, name: str) -> str:
    """The `<name>_display` string without a minus sign (the sentence says 'above' or 'below' instead)."""
    return str(facts[f"{name}_display"]).replace("-", "")


# ---- offline templates: each takes the display facts and the formatted amount at risk ----

def _duplicate(f: dict, risk: str) -> str:
    return (f"Invoice {f['invoice_no']} from {f['vendor']} repeats {f['original_invoice_no']} for "
            f"{f['total_display']}, so paying both would pay the vendor twice.")


def _missing_po(f: dict, risk: str) -> str:
    start = f"Invoice {f['invoice_no']} from {f['vendor']}"
    if f.get("po_ref_given"):
        return (f"{start} quotes {f['po_ref_given']}, which does not exist in the PO file, "
                f"so there is no approved order behind this {risk} bill.")
    return f"{start} quotes no PO and none matches, so there is no approved order behind this {risk} bill."


def _vendor_name_mismatch(f: dict, risk: str) -> str:
    return (f"Invoice {f['invoice_no']} is billed by {f['invoice_vendor']} but {f['po_no']} was raised to "
            f"{f['po_vendor']}, so the vendor master probably holds one supplier under two names.")


def _amount_mismatch(f: dict, risk: str) -> str:
    direction = "above" if float(f["diff"]) > 0 else "below"
    gst = float(f.get("gst_rate") or 0)
    with_gst = f", or {risk} with {_num(gst)}% GST" if gst > 0 else ""  # the amount at risk includes the tax
    return (f"Invoice {f['invoice_no']} from {f['vendor']} charges {f['invoice_rate_display']} a unit against "
            f"{f['po_rate_display']} on {f['po_no']}, so the {_num(f['qty'])} units are billed "
            f"{_abs_money(f, 'diff')} {direction} the agreed price{with_gst}.")


def _quantity_mismatch(f: dict, risk: str) -> str:
    return (f"Invoice {f['invoice_no']} from {f['vendor']} takes billing on {f['po_no']} to "
            f"{_num(f['invoiced_qty'])} units against {_num(f['po_qty'])} ordered, so "
            f"{_num(f['excess_qty'])} units are billed beyond the order.")


GST_REASONS: dict[str, Callable[[dict, str], str]] = {
    "missing": lambda f, risk: ("shows no GSTIN even though it charges GST, "
                                "so the tax on it may not be claimable as credit"),
    "format": lambda f, risk: (f"carries GSTIN {f['gstin']}, which is not a valid GSTIN format, "
                               "so the input tax credit may be rejected"),
    "checksum": lambda f, risk: (f"carries GSTIN {f['gstin']}, which fails the GSTIN check digit, "
                                 "so it is probably mistyped and the input tax credit could be rejected"),
    "slab": lambda f, risk: (f"charges GST at {_num(f['gst_rate'])}%, which is not a valid GST rate, "
                             "so the tax needs correcting before it is claimed"),
    "rate_vs_po": lambda f, risk: (f"charges GST at {_num(f['gst_rate'])}% while {f['po_no']} was raised at "
                                   f"{_num(f['po_gst_rate'])}%, so {risk} of the tax charged is not what was agreed"),
    "amount": lambda f, risk: (f"shows a GST amount that is not {_num(f['gst_rate'])}% of the taxable value, "
                               "so the tax total is wrong"),
}


def _gst_error(f: dict, risk: str) -> str:
    reason = GST_REASONS.get(f.get("reason"), GST_REASONS["amount"])
    return f"Invoice {f['invoice_no']} from {f['vendor']} {reason(f, risk)}."


def _partial_payment(f: dict, risk: str) -> str:
    tds = f" plus {f['tds_display']} TDS" if float(f["tds"]) > 0 else ""
    return (f"Invoice {f['invoice_no']} from {f['vendor']} is for {f['total_display']} but only "
            f"{f['paid_display']} was paid{tds}, so {f['outstanding_display']} is still outstanding.")


def _overpayment(f: dict, risk: str) -> str:
    tds = f" plus {f['tds_display']} TDS" if float(f["tds"]) > 0 else ""
    if f.get("paid_twice"):
        return (f"Invoice {f['invoice_no']} from {f['vendor']} was paid twice: {f['paid_display']}{tds} went out "
                f"against a {f['total_display']} invoice, so {f['excess_display']} is due back from the vendor.")
    return (f"Invoice {f['invoice_no']} from {f['vendor']} is for {f['total_display']} but "
            f"{f['paid_display']} was paid{tds}, so the vendor holds {f['excess_display']} too much.")


TEMPLATES: dict[str, Callable[[dict, str], str]] = {
    "duplicate_invoice": _duplicate,
    "missing_po": _missing_po,
    "vendor_name_mismatch": _vendor_name_mismatch,
    "amount_mismatch": _amount_mismatch,
    "quantity_mismatch": _quantity_mismatch,
    "gst_error": _gst_error,
    "partial_payment": _partial_payment,
    "overpayment": _overpayment,
}


def offline_sentence(exc: TriageException) -> str:
    return TEMPLATES[exc.type](_display_facts(exc.facts), format_inr(exc.amount_at_risk))


# ---- what each exception means, for the model prompt ----

GST_MEANINGS = {
    "missing": "The invoice charges GST but shows no supplier GSTIN.",
    "format": "The supplier GSTIN is not in a valid GSTIN format.",
    "checksum": "The supplier GSTIN has a valid format but fails the GSTIN check digit, so it is probably mistyped.",
    "slab": "The GST rate on the invoice is not one of the valid GST rates.",
    "rate_vs_po": "The GST rate on the invoice differs from the GST rate on the purchase order.",
    "amount": "The GST amount on the invoice does not equal the taxable value times the GST rate.",
}
MEANINGS = {
    "duplicate_invoice": "The invoice repeats an earlier invoice (original_invoice_no) from the same vendor with "
                         "the same total, so paying both would pay the vendor twice.",
    "vendor_name_mismatch": "The vendor name on the invoice differs from the vendor name on the linked PO, "
                            "though it is probably the same supplier. No money is at risk yet.",
    "amount_mismatch": "The unit rate billed differs from the rate agreed on the PO, so the invoice charges "
                       "diff more or less than it should.",
    "quantity_mismatch": "The total quantity invoiced against the PO is more than the quantity ordered.",
    "partial_payment": "The payments made against the invoice, counting TDS, add up to less than the invoice total.",
    "overpayment": "The payments made against the invoice, counting TDS, add up to more than the invoice total.",
}


def meaning(exc: TriageException) -> str:
    """One line saying what this exception type means, so the model does not have to guess."""
    if exc.type == "missing_po":
        if exc.facts.get("po_ref_given"):
            return f"The invoice quotes PO {exc.facts['po_ref_given']}, but no such PO exists in the PO file."
        return "The invoice quotes no PO and none could be matched."
    if exc.type == "gst_error":
        return GST_MEANINGS.get(exc.facts.get("reason"), GST_MEANINGS["amount"])
    if exc.type == "amount_mismatch" and float(exc.facts.get("gst_rate") or 0) > 0:
        return (MEANINGS[exc.type] + f" The amount at risk is that difference plus {_num(exc.facts['gst_rate'])}% "
                "GST, so say that the amount at risk includes GST.")
    if exc.type == "overpayment" and exc.facts.get("paid_twice"):
        return MEANINGS[exc.type] + " It was paid exactly twice, so say the invoice was paid twice."
    return MEANINGS[exc.type]


# ---- fingerprint and validation ----

def fingerprint(exc: TriageException) -> str:
    """Cache key: the type and facts only, so it survives a change of exception ids."""
    payload = json.dumps({"type": exc.type, "facts": exc.facts}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _amount_value(text: str) -> float:
    return round(float(text.replace(",", "")), 2)


def _allowed_amounts(facts: dict, amount_at_risk: float | None) -> set[float]:
    """Every rupee value a sentence may mention: the displayed facts, their raw values and the amount at risk."""
    shown = _display_facts(facts)
    allowed = {_amount_value(m) for v in shown.values() if isinstance(v, str) for m in RUPEE_AMOUNT.findall(v)}
    allowed |= {round(float(v), 2) for k, v in facts.items()
                if f"{k}_display" in facts and isinstance(v, (int, float))}
    if amount_at_risk is not None:
        allowed |= {round(amount_at_risk, 2), _amount_value(format_inr(amount_at_risk)[1:])}
    return allowed


def _required_words(facts: dict) -> list[str]:
    """Words a sentence must contain so it matches the amount at risk: GST when the risk includes it, and twice."""
    words = []
    if "invoice_rate" in facts and float(facts.get("gst_rate") or 0) > 0:
        words.append("gst")
    if facts.get("paid_twice"):
        words.append("twice")
    return words


def is_valid_sentence(text: str, facts: dict, amount_at_risk: float | None = None) -> bool:
    """One plain line in British English, at most 300 chars, ends with a full stop, names the invoice, invents no
    rupee amount, and says GST or paid twice when the facts need it."""
    if not isinstance(text, str) or not text or "\n" in text or "\r" in text:
        return False
    if len(text) > MAX_SENTENCE_CHARS or not text.endswith("."):
        return False
    invoice_no = re.escape(str(facts["invoice_no"]))
    number = re.compile(rf"(?<![A-Za-z0-9]){invoice_no}(?![A-Za-z0-9]|[-/][A-Za-z0-9])")
    if not number.search(text):
        return False
    body = number.sub(" ", text)
    if any(mark in body for mark in "*`#") or AMERICAN_SPELLING.search(text):
        return False
    if not all(word in text.lower() for word in _required_words(facts)):
        return False
    allowed = _allowed_amounts(facts, amount_at_risk)
    mentioned = RUPEE_AMOUNT.findall(body) + RUPEES_WORD.findall(body)
    return all(_amount_value(m) in allowed for m in mentioned)


def _entry_ok(entry: dict, exc: TriageException) -> bool:
    return is_valid_sentence(entry["text"], exc.facts, exc.amount_at_risk)


# ---- model backends ----

def build_prompt(exc: TriageException) -> str:
    """Deterministic prompt from the exception type, its facts and its amount at risk. No other row data."""
    invoice_no = exc.facts["invoice_no"]
    return (
        "You write one-line explanations for a finance team reviewing invoice exceptions.\n"
        "Write one sentence, under 40 words, in plain English for a finance user, starting with "
        f'"Invoice {invoice_no}". State the finding and its money impact.\n'
        "Rules: use the ₹ amounts exactly as given in the facts. Do not introduce any new numbers. "
        "Use British spelling (authorisation, not authorization). No markdown. Reply with the sentence only.\n"
        f"Exception type: {exc.type}\n"
        f"Meaning: {meaning(exc)}\n"
        f"Amount at risk: {format_inr(exc.amount_at_risk)}\n"
        f"Facts: {json.dumps(_display_facts(exc.facts), sort_keys=True, ensure_ascii=False)}\n"
    )


def run_claude_cli(prompt: str) -> str:
    """Ask `claude -p`. It runs from the temp folder so it does not load this repo's CLAUDE.md and hooks."""
    result = subprocess.run(["claude", "-p", "--model", "haiku"], input=prompt, capture_output=True, text=True,
                            timeout=CLI_TIMEOUT_S, check=True, cwd=tempfile.gettempdir())
    return result.stdout.strip()


def run_anthropic(prompt: str) -> str:
    body = json.dumps({"model": API_MODEL, "max_tokens": 120,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
    })
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
        data = json.loads(response.read())
    return "".join(block.get("text", "") for block in data["content"]).strip()


def pick_runner(backend: str, runner: Runner | None) -> Runner | None:
    """The function that asks a model, or None when we should stay offline."""
    if backend not in BACKENDS:
        raise ValueError(f"Unknown explainer backend {backend!r}. Use one of: {', '.join(BACKENDS)}.")
    if backend == "offline":
        return None
    if runner is not None:
        return runner
    if backend == "claude-cli":
        if shutil.which("claude"):
            return run_claude_cli
        warn("the claude command is not on PATH, using offline explanations.")
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return run_anthropic
    warn("ANTHROPIC_API_KEY is not set, using offline explanations.")
    return None


def _ask(runner: Runner, exc: TriageException) -> str | None:
    """A validated model sentence, or None on any error, timeout or bad output."""
    try:
        text = runner(build_prompt(exc)).strip()
    except Exception as error:  # a flaky model must never break the report
        warn(f"{exc.facts['invoice_no']}: {type(error).__name__}, using the offline sentence.")
        return None
    return text if is_valid_sentence(text, exc.facts, exc.amount_at_risk) else None


# ---- cache ----

def load_cache(path: Path) -> dict[str, dict[str, str]]:
    """Entries by fingerprint. A missing or corrupt file is simply an empty cache."""
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(entries, dict):
        return {}
    return {fp: e for fp, e in entries.items() if isinstance(e, dict) and isinstance(e.get("text"), str)}


def save_cache(path: Path, entries: dict[str, dict[str, str]]) -> None:
    """Write to a temp file in the same folder, then swap it in, so a crash cannot leave half a file."""
    path = Path(path)
    text = json.dumps({"version": 1, "entries": entries}, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    tmp_name = None
    try:
        handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except OSError as error:
        warn(f"could not write the cache file ({error.strerror or error}).")
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


def cached_count(excs: list[TriageException], cache_path: Path = DEFAULT_CACHE) -> int:
    """How many of these exceptions have an accepted model sentence in the cache."""
    entries = load_cache(cache_path)
    return sum(1 for exc in excs if _cached_text(entries, exc) is not None)


def _cached_text(entries: dict, exc: TriageException) -> str | None:
    entry = entries.get(fingerprint(exc))
    return entry["text"] if entry and _entry_ok(entry, exc) else None


# ---- public API ----

def explain_all(excs: list[TriageException], backend: str = "offline", cache_path: Path = DEFAULT_CACHE,
                refresh: bool = False, runner: Runner | None = None) -> dict[str, str]:
    """Map exception id to a one-sentence explanation."""
    active = pick_runner(backend, runner)
    entries = load_cache(cache_path)
    texts = {exc.id: _cached_text(entries, exc) for exc in excs}
    todo = [exc for exc in excs if active and (refresh or texts[exc.id] is None)]
    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            answers = list(pool.map(lambda exc: _ask(active, exc), todo))
        for exc, text in zip(todo, answers, strict=True):
            if text:
                texts[exc.id] = text
                entries[fingerprint(exc)] = {"text": text, "backend": backend, "type": exc.type}
    if active:
        current = {fingerprint(exc): exc for exc in excs} if refresh else None
        _save_if_changed(cache_path, entries, current)
    return {exc.id: texts[exc.id] or offline_sentence(exc) for exc in excs}


def _save_if_changed(cache_path: Path, entries: dict, current: dict[str, TriageException] | None) -> None:
    """Persist new entries. On a refresh (`current` set), also drop entries that are stale or fail validation."""
    if current is not None:
        entries = {fp: e for fp, e in entries.items() if fp in current and _entry_ok(e, current[fp])}
    if entries != load_cache(cache_path):
        save_cache(cache_path, entries)


def refresh_cache(excs: list[TriageException], backend: str, cache_path: Path = DEFAULT_CACHE,
                  runner: Runner | None = None) -> tuple[int, int]:
    """Regenerate every explanation with the model. Returns (accepted, total) for run.py to print."""
    explain_all(excs, backend=backend, cache_path=cache_path, refresh=True, runner=runner)
    return cached_count(excs, cache_path), len(excs)
