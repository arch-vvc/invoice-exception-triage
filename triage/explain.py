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
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .engine import TriageException

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


def warn(message: str) -> None:
    print(f"explain: {message}", file=sys.stderr)


# ---- offline templates ----

def _num(value) -> str:
    """12.0 -> '12', 12.5 -> '12.5', so quantities and rates read naturally."""
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _abs_money(facts: dict, name: str) -> str:
    """The `<name>_display` string without a minus sign (the sentence says 'above' or 'below' instead)."""
    return str(facts[f"{name}_display"]).replace("-", "")


def _duplicate(f: dict) -> str:
    return (f"Invoice {f['invoice_no']} from {f['vendor']} repeats {f['original_invoice_no']} for "
            f"{f['total_display']}, so paying both would pay the vendor twice.")


def _missing_po(f: dict) -> str:
    if f.get("po_ref_given"):
        return (f"Invoice {f['invoice_no']} from {f['vendor']} quotes {f['po_ref_given']}, which is not in the "
                "purchase order list, so there is no approved order behind this bill.")
    return (f"Invoice {f['invoice_no']} from {f['vendor']} has no PO reference and no single order to match it "
            "to, so nobody has approved this spend.")


def _vendor_name_mismatch(f: dict) -> str:
    return (f"Invoice {f['invoice_no']} is billed by {f['invoice_vendor']} but {f['po_no']} was raised to "
            f"{f['po_vendor']}, so the vendor master probably holds one supplier under two names.")


def _amount_mismatch(f: dict) -> str:
    direction = "above" if float(f["diff"]) > 0 else "below"
    return (f"Invoice {f['invoice_no']} from {f['vendor']} charges {f['invoice_rate_display']} a unit against "
            f"{f['po_rate_display']} on {f['po_no']}, so the {_num(f['qty'])} units are billed "
            f"{_abs_money(f, 'diff')} {direction} the agreed price.")


def _quantity_mismatch(f: dict) -> str:
    return (f"Invoice {f['invoice_no']} from {f['vendor']} takes billing on {f['po_no']} to "
            f"{_num(f['invoiced_qty'])} units against {_num(f['po_qty'])} ordered, so "
            f"{_num(f['excess_qty'])} units are billed beyond the order.")


GST_REASONS = {
    "missing": lambda f: "shows no GSTIN even though it charges GST, so the tax on it may not be claimable as credit",
    "format": lambda f: (f"carries GSTIN {f['gstin']}, which is not a valid GSTIN format, "
                         "so the input tax credit may be rejected"),
    "checksum": lambda f: (f"carries GSTIN {f['gstin']}, which fails the GSTIN check digit, "
                           "so it is probably mistyped and the input tax credit could be rejected"),
    "slab": lambda f: (f"charges GST at {_num(f['gst_rate'])}%, which is not a valid GST rate, "
                       "so the tax needs correcting before it is claimed"),
    "rate_vs_po": lambda f: (f"charges GST at {_num(f['gst_rate'])}% while {f['po_no']} was raised at "
                             f"{_num(f['po_gst_rate'])}%, so the tax charged is not what was agreed"),
    "amount": lambda f: (f"shows a GST amount that is not {_num(f['gst_rate'])}% of the taxable value, "
                         "so the tax total is wrong"),
}


def _gst_error(f: dict) -> str:
    reason = GST_REASONS.get(f.get("reason"), GST_REASONS["amount"])
    return f"Invoice {f['invoice_no']} from {f['vendor']} {reason(f)}."


def _partial_payment(f: dict) -> str:
    tds = f" plus {f['tds_display']} TDS" if float(f["tds"]) > 0 else ""
    return (f"Invoice {f['invoice_no']} from {f['vendor']} is for {f['total_display']} but only "
            f"{f['paid_display']} was paid{tds}, so {f['outstanding_display']} is still outstanding.")


def _overpayment(f: dict) -> str:
    tds = f" plus {f['tds_display']} TDS" if float(f["tds"]) > 0 else ""
    return (f"Invoice {f['invoice_no']} from {f['vendor']} is for {f['total_display']} but "
            f"{f['paid_display']} was paid{tds}, so the vendor holds {f['excess_display']} too much.")


TEMPLATES: dict[str, Callable[[dict], str]] = {
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
    return TEMPLATES[exc.type](exc.facts)


# ---- fingerprint and validation ----

def fingerprint(exc: TriageException) -> str:
    """Cache key: the type and facts only, so it survives a change of exception ids."""
    payload = json.dumps({"type": exc.type, "facts": exc.facts}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _amount_digits(text: str) -> str:
    return text.rstrip(",").replace(",", "")


def _allowed_amounts(facts: dict) -> set[str]:
    allowed: set[str] = set()
    for name, value in facts.items():
        if name.endswith("_display"):
            allowed.update(_amount_digits(m) for m in RUPEE_AMOUNT.findall(str(value)))
    return allowed


def is_valid_sentence(text: str, facts: dict) -> bool:
    """One line, at most 300 chars, ends with a full stop, names the invoice and invents no rupee amount."""
    if not isinstance(text, str) or not text or "\n" in text or "\r" in text:
        return False
    if len(text) > MAX_SENTENCE_CHARS or not text.endswith("."):
        return False
    if str(facts["invoice_no"]) not in text:
        return False
    allowed = _allowed_amounts(facts)
    return all(_amount_digits(m) in allowed for m in RUPEE_AMOUNT.findall(text))


# ---- model backends ----

def build_prompt(exc: TriageException) -> str:
    """Deterministic prompt from the exception type and facts only. No other row data is sent."""
    return (
        "You write one-line explanations for a finance team reviewing invoice exceptions.\n"
        "Write one sentence, under 40 words, in plain English that explains the problem below.\n"
        "Rules: mention the invoice number exactly as given. Use the ₹ amounts exactly as given in the "
        "*_display facts. Do not introduce any new numbers. Reply with the sentence only.\n"
        f"Exception type: {exc.type}\n"
        f"Facts: {json.dumps(exc.facts, sort_keys=True, ensure_ascii=False)}\n"
    )


def run_claude_cli(prompt: str) -> str:
    result = subprocess.run(["claude", "-p", "--model", "haiku"], input=prompt, capture_output=True, text=True,
                            timeout=CLI_TIMEOUT_S, check=True)
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
    return text if is_valid_sentence(text, exc.facts) else None


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
    """Write atomically so a crash mid-write cannot leave half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps({"version": 1, "entries": entries}, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as error:
        warn(f"could not write the cache file ({error.strerror or error}).")
        tmp.unlink(missing_ok=True)


def cached_count(excs: list[TriageException], cache_path: Path = DEFAULT_CACHE) -> int:
    """How many of these exceptions have an accepted model sentence in the cache."""
    entries = load_cache(cache_path)
    return sum(1 for exc in excs if _cached_text(entries, exc) is not None)


def _cached_text(entries: dict, exc: TriageException) -> str | None:
    entry = entries.get(fingerprint(exc))
    if entry and is_valid_sentence(entry["text"], exc.facts):
        return entry["text"]
    return None


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
        _save_if_changed(cache_path, entries, {fingerprint(e) for e in excs} if refresh else None)
    return {exc.id: texts[exc.id] or offline_sentence(exc) for exc in excs}


def _save_if_changed(cache_path: Path, entries: dict, keep: set[str] | None) -> None:
    """Persist new entries. With `keep` set (a refresh), entries for other exceptions are pruned."""
    if keep is not None:
        entries = {fp: e for fp, e in entries.items() if fp in keep}
    if entries != load_cache(cache_path):
        save_cache(cache_path, entries)


def refresh_cache(excs: list[TriageException], backend: str, cache_path: Path = DEFAULT_CACHE,
                  runner: Runner | None = None) -> tuple[int, int]:
    """Regenerate every explanation with the model. Returns (accepted, total) for run.py to print."""
    explain_all(excs, backend=backend, cache_path=cache_path, refresh=True, runner=runner)
    return cached_count(excs, cache_path), len(excs)
