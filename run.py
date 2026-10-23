"""Run the exception triage demo: serve the dashboard, or check the sample data from the terminal.

    python demos/exception-triage/run.py              serve the dashboard on http://127.0.0.1:8765
    python demos/exception-triage/run.py --check      triage the sample data and compare it with the answer key
    python demos/exception-triage/run.py --refresh --explainer claude-cli
                                                      rewrite the cached explanations with a model

Standard library only. The server itself never calls a model.
"""
import argparse
import csv
import functools
import signal
import sys
import time
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_DIR))

from triage.engine import triage_dir  # noqa: E402
from triage.explain import cached_count, explain_all, refresh_cache  # noqa: E402
from triage.normalise import format_inr  # noqa: E402
from triage.server import make_server  # noqa: E402

SAMPLE_DIR = DEMO_DIR / "sample_data"
ANSWER_KEY = SAMPLE_DIR / "expected_exceptions.csv"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exception triage for invoices, purchase orders and payments.")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    parser.add_argument("--check", action="store_true",
                        help="triage the sample data, compare it with the answer key, print a summary and exit")
    parser.add_argument("--refresh", action="store_true",
                        help="rewrite the cached explanations of the sample exceptions with a model "
                             "(needs --explainer claude-cli or anthropic)")
    parser.add_argument("--explainer", choices=["offline", "claude-cli", "anthropic"], default="offline",
                        help="which model writes the cached sentences for --refresh (default offline)")
    args = parser.parse_args(argv)
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
    print(f"explanations cached: {cached_count(result.exceptions)}/{count}")
    print(f"answer key: {len(key) - len(missing)}/{len(key)} matched, {len(extra)} extra")
    for kind, invoice_no in sorted(missing):
        print(f"  missing: {kind} {invoice_no}")
    for kind, invoice_no in sorted(extra):
        print(f"  extra: {kind} {invoice_no}")
    print(f"done in {time.monotonic() - started:.1f}s")
    return 0 if not missing and not extra else 1


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
