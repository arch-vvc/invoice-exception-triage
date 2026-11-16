"""Run the exception triage demo: serve the dashboard, or check the sample data from the terminal.

    python run.py              serve the dashboard on http://127.0.0.1:8765
    python run.py --check      triage the sample data and compare it with the answer key
    python run.py --check --dir <folder> [--csv report.csv]
                               triage your own invoices, PO and payment CSV files in a folder
    python run.py --refresh --explainer claude-cli
                                                      rewrite the cached explanations with a model

Standard library only. The server itself never calls a model.
"""
import argparse
import csv
import functools
import re
import signal
import sys
import time
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_DIR))

from triage.engine import triage_bytes, triage_dir  # noqa: E402
from triage.explain import cached_count, explain_all, refresh_cache  # noqa: E402
from triage.loader import InputError  # noqa: E402
from triage.normalise import format_inr  # noqa: E402
from triage.report import TYPE_LABELS, to_csv_bytes  # noqa: E402
from triage.server import make_server  # noqa: E402

SAMPLE_DIR = DEMO_DIR / "sample_data"
ANSWER_KEY = SAMPLE_DIR / "expected_exceptions.csv"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exception triage for invoices, purchase orders and payments.")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    parser.add_argument("--check", action="store_true",
                        help="triage the sample data, compare it with the answer key, print a summary and exit")
    parser.add_argument("--dir", metavar="FOLDER",
                        help="with --check: triage the three CSV files in this folder instead of the sample data")
    parser.add_argument("--csv", metavar="FILE", help="with --check --dir: also write the full report to this file")
    parser.add_argument("--refresh", action="store_true",
                        help="rewrite the cached explanations of the sample exceptions with a model "
                             "(needs --explainer claude-cli or anthropic)")
    parser.add_argument("--explainer", choices=["offline", "claude-cli", "anthropic"], default="offline",
                        help="which model writes the cached sentences for --refresh (default offline)")
    args = parser.parse_args(argv)
    if (args.dir or args.csv) and not args.check:
        parser.error("--dir and --csv only apply to --check.")
    if args.csv and not args.dir:
        parser.error("--csv needs --dir. Example: --check --dir my_exports --csv report.csv")
    if args.refresh and args.explainer == "offline":
        parser.error("--refresh needs a model. Add --explainer claude-cli (uses your Claude Code login) "
                     "or --explainer anthropic (uses ANTHROPIC_API_KEY).")
    if args.explainer != "offline" and not args.refresh:
        parser.error("--explainer only applies to --refresh. The dashboard always shows cached or offline "
                     "explanations, so uploaded files never start a model call.")
    return args


def read_answer_key() -> set[tuple[str, str]]:
    """(type, invoice_no) for every planted exception."""
    with ANSWER_KEY.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    start = next(i for i, row in enumerate(rows) if row and row[0] == "exception_type")  # line 1 is a marker
    header = rows[start]
    records = (dict(zip(header, row, strict=False)) for row in rows[start + 1:] if any(row))
    return {(rec["exception_type"], rec["invoice_no"].strip()) for rec in records}


def compare_with_key(found: set[tuple[str, str]], key: set[tuple[str, str]]):
    """(missing, extra): planted exceptions the engine did not find, and findings that were not planted."""
    return key - found, found - key


def check() -> int:
    started = time.monotonic()
    result = triage_dir(SAMPLE_DIR)
    key = read_answer_key()
    found = {(exc.type, exc.invoice_ids[0].strip()) for exc in result.exceptions}
    missing, extra = compare_with_key(found, key)
    count = len(result.exceptions)
    print(f"invoices checked: {result.invoices_checked}")
    print(f"exceptions found: {count}")
    print(f"amount at risk: {format_inr(result.amount_at_risk)}")
    print(f"across all findings: {format_inr(result.amount_at_risk_findings)}")
    print(f"explanations cached: {cached_count(result.exceptions)}/{count}")
    print(f"answer key: {len(key) - len(missing)}/{len(key)} matched, {len(extra)} extra")
    for kind, invoice_no in sorted(missing):
        print(f"  missing: {kind} {invoice_no}")
    for kind, invoice_no in sorted(extra):
        print(f"  extra: {kind} {invoice_no}")
    print(f"done in {time.monotonic() - started:.1f}s")
    return 0 if not missing and not extra else 1


FILE_PATTERNS = {  # kind -> how its file name must look, after "_" and "-" are read as spaces
    "invoices": re.compile(r"invoice|purchase register"),
    "purchase_orders": re.compile(r"\bpo\b|purchase order"),
    "payments": re.compile(r"payment"),
}
TOP_FINDINGS = 5


def find_export_files(folder: Path) -> dict[str, tuple[str, bytes]]:
    """Pick the invoice, PO and payment CSV in a folder by name. Raises InputError if one is missing or ambiguous."""
    names = sorted((f.name for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".csv"), key=str.lower)
    matches: dict[str, list[str]] = {kind: [] for kind in FILE_PATTERNS}
    for name in names:
        words = re.sub(r"[_\-]+", " ", name.lower())
        kind = next((k for k, pattern in FILE_PATTERNS.items() if pattern.search(words)), None)
        if kind:
            matches[kind].append(name)
    seen = ", ".join(names) or "no CSV files"
    for kind, found in matches.items():
        if not found:
            raise InputError(f"No {kind.replace('_', ' ')} file found in {folder}. CSV files seen: {seen}. "
                             "Name them with invoice (or purchase register), PO (or purchase order) and payment.")
        if len(found) > 1:
            raise InputError(f"More than one {kind.replace('_', ' ')} file found in {folder}: {', '.join(found)}. "
                             "Keep one in the folder.")
    return {kind: (found[0], (folder / found[0]).read_bytes()) for kind, found in matches.items()}


def check_folder(folder: str, csv_path: str | None) -> int:
    path = Path(folder)
    if not path.is_dir():
        print(f"Folder not found: {folder}", file=sys.stderr)
        return 2
    try:
        result = triage_bytes(find_export_files(path))
    except InputError as error:
        print(str(error), file=sys.stderr)
        return 2
    count = len(result.exceptions)
    print(f"invoices checked: {result.invoices_checked}")
    print(f"exceptions found: {count}")
    print(f"amount at risk: {format_inr(result.amount_at_risk)}")
    print(f"across all findings: {format_inr(result.amount_at_risk_findings)}")
    print(f"explanations cached: {cached_count(result.exceptions)}/{count}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    explanations = explain_all(result.exceptions, backend="offline")
    if csv_path:
        Path(csv_path).write_bytes(to_csv_bytes(result.exceptions, explanations))
        print(f"report written to {csv_path}")
    if not count:
        print("no exceptions found")
        return 0
    print(f"\nLargest findings (top {min(TOP_FINDINGS, count)} of {count}):")
    for exc in result.exceptions[:TOP_FINDINGS]:
        label = TYPE_LABELS.get(exc.type, exc.type.replace("_", " ").capitalize())
        print(f"  {label} | {exc.facts.get('invoice_no', '')} | {exc.facts.get('vendor', '')} | "
              f"{format_inr(exc.amount_at_risk)} | {explanations.get(exc.id, '')}")
    return 0


def refresh(backend: str) -> int:
    result = triage_dir(SAMPLE_DIR)
    print(f"Asking {backend} to explain {len(result.exceptions)} exceptions. This can take a minute.")
    done, total = refresh_cache(result.exceptions, backend)
    print(f"explanations cached: {done}/{total}")
    if done < total:
        print("Some explanations fell back to offline text. Run --refresh again to retry them.")
    return 0 if done == total else 1


def stop_on_sigterm(signum, frame):
    raise KeyboardInterrupt


def serve(host: str, port: int) -> int:
    # Always offline: cached model sentences still show for the sample, and uploaded files
    # never start a model call or write to the committed cache.
    explainer = functools.partial(explain_all, backend="offline")
    try:
        server = make_server(host, port, SAMPLE_DIR, explainer)
    except OSError as error:
        print(f"Could not listen on {host}:{port} ({error.strerror or error}). "
              f"Is another copy running? Try --port {port + 1}.", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    print(f"Exception triage dashboard: http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # so the rupee sign prints on any console
    args = parse_args(argv)
    if args.check and args.dir:
        return check_folder(args.dir, args.csv)
    if args.check:
        return check()
    if args.refresh:
        return refresh(args.explainer)
    return serve(args.host, args.port)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
