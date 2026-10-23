"""Local web server: the dashboard page plus a small JSON API. Standard library only, one user, no auth.

    GET  /                      the dashboard (the only file this server ever serves)
    POST /api/upload/<kind>     raw CSV bytes in the body, display name in the X-Filename header
    POST /api/triage            run triage on the three uploaded files
    GET  /api/sample            run triage on the bundled sample data
    GET  /api/report.csv        the last result as a CSV download (400 until a triage has run)

Uploads and the last result live in memory on the server object and are lost when it stops.
"""
import json
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .engine import SAMPLE_FILES, TriageException, TriageResult, triage_bytes, triage_dir
from .loader import InputError, load_table
from .normalise import format_inr
from .report import to_csv

INDEX_HTML = Path(__file__).resolve().parent / "web" / "index.html"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024  # of a rejected upload we read and discard, so the browser sees the 413
FILENAME_LIMIT = 120
GENERIC_ERROR = "Something went wrong on our side. Check the terminal for details."
EXPLAIN_WARNING = "Explanations are unavailable right now, so the table shows the numbers without them."
CONTENT_SECURITY_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                           "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")

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


class TriageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, sample_dir, explainer):
        super().__init__(address, handler)
        self.sample_dir = Path(sample_dir)
        self.explainer = explainer
        self.lock = threading.Lock()
        self.uploads: dict[str, tuple[str, bytes]] = {}  # kind -> (display name, bytes)
        self.last: tuple[list[TriageException], dict[str, str]] | None = None  # for the CSV download


def make_server(host: str, port: int, sample_dir, explainer) -> TriageServer:
    """Build the server without starting it. `explainer` maps a list of exceptions to {id: sentence}."""
    return TriageServer((host, port), Handler, sample_dir, explainer)


# ---- result JSON ----

def explain(server: TriageServer, result: TriageResult) -> tuple[dict[str, str], list[str]]:
    """Ask the explainer for sentences. If it fails, carry on with blanks and a warning."""
    try:
        found = server.explainer(result.exceptions) or {}
        return {exc.id: str(found.get(exc.id, "")) for exc in result.exceptions}, []
    except Exception:
        traceback.print_exc()
        return {}, [EXPLAIN_WARNING]


def build_result(result: TriageResult, explanations: dict[str, str], filenames: dict[str, str], source: str) -> dict:
    """The JSON the dashboard draws. Money arrives as ready-made *_display strings."""
    records = {
        "invoices": {r.source_row: r for r in result.invoices},
        "purchase_orders": {r.source_row: r for r in result.purchase_orders},
        "payments": {r.source_row: r for r in result.payments},
    }
    exceptions = []
    for exc in result.exceptions:
        rows = {kind: [{"row": n, "cells": dict(records[kind][n].raw)} for n in exc.source_rows.get(kind, [])
                       if n in records[kind]]
                for kind in records}
        exceptions.append({
            "id": exc.id,
            "type": exc.type,
            "type_label": TYPE_LABELS.get(exc.type, exc.type.replace("_", " ").capitalize()),
            "severity": exc.severity,
            "invoice_no": str(exc.facts.get("invoice_no", "")),
            "vendor": str(exc.facts.get("vendor", "")),
            "po_no": str(exc.facts.get("po_no", "")),
            "amount_at_risk": exc.amount_at_risk,
            "amount_at_risk_display": format_inr(exc.amount_at_risk),
            "suggested_action": exc.suggested_action,
            "explanation": explanations.get(exc.id, ""),
            "rows": rows,
        })
    by_type: dict[str, int] = {}
    for exc in exceptions:
        by_type[exc["type"]] = by_type.get(exc["type"], 0) + 1
    return {
        "source": source,
        "totals": {
            "invoices_checked": result.invoices_checked,
            "exceptions_found": len(exceptions),
            "amount_at_risk": result.amount_at_risk,
            "amount_at_risk_display": format_inr(result.amount_at_risk),
            "by_type": by_type,
            "high_count": sum(1 for e in exceptions if e["severity"] == "high"),
        },
        "exceptions": exceptions,
        "warnings": list(result.warnings),
        "files": {kind: {"filename": filenames[kind], "header": list(next(iter(table.values())).raw)
                         if table else []}
                  for kind, table in records.items()},
    }


def clean_filename(raw: str, default: str) -> str:
    """A name for display only: percent-decoded, no folders, no control characters, not too long."""
    name = re.split(r"[\\/]", unquote(raw or ""))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if name in ("", ".", ".."):
        return default
    return name[:FILENAME_LIMIT]


# ---- request handling ----

class Handler(BaseHTTPRequestHandler):
    server: TriageServer
    timeout = 30  # seconds of silence before a stuck connection is dropped

    # -- plumbing --

    def log_request(self, code="-", size="-"):
        sys.stderr.write(f"{self.command} {urlsplit(self.path).path[:80]} -> {code}\n")

    def send_bytes(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict, extra: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8", extra)

    def send_error_json(self, status: int, message: str, extra: dict[str, str] | None = None):
        self.send_json(status, {"error": message}, extra)

    def guarded(self, action):
        """Run a route. Our own InputError reaches the user as a 400, anything else as a generic 500."""
        try:
            action()
        except InputError as err:
            self.send_error_json(400, str(err))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away
        except Exception:
            traceback.print_exc()
            self.send_error_json(500, GENERIC_ERROR)

    def do_GET(self):
        path = urlsplit(self.path).path
        routes = {"/": self.serve_index, "/api/sample": self.run_sample, "/api/report.csv": self.send_report}
        self.route(path, routes, post_only=("/api/triage",))

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/upload/"):
            kind = path[len("/api/upload/"):]
            if kind in SAMPLE_FILES:
                return self.guarded(lambda: self.receive_upload(kind))
            return self.send_error_json(404, f"Unknown file type '{kind[:40]}'. Use invoices, purchase_orders "
                                             "or payments.")
        self.route(path, {"/api/triage": self.run_uploaded}, get_only=("/", "/api/sample", "/api/report.csv"))

    def route(self, path, routes, get_only=(), post_only=()):
        if path in routes:
            return self.guarded(routes[path])
        if path in get_only or path in post_only:
            allowed = "POST" if path in post_only else "GET"
            return self.send_error_json(405, f"Use {allowed} for this address.", {"Allow": allowed})
        self.send_error_json(404, "Not found.")

    # -- routes --

    def serve_index(self):
        self.send_bytes(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8",
                        {"Content-Security-Policy": CONTENT_SECURITY_POLICY})

    def receive_upload(self, kind: str):
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            return self.send_error_json(411, "The upload did not say how big it is. Try again.")
        if not length_text.isdigit():
            return self.send_error_json(400, "The upload size was not a number. Try again.")
        length = int(length_text)
        if length > MAX_UPLOAD_BYTES:
            self.send_error_json(413, "That file is over 5 MB, which is more than this demo accepts. "
                                      "Export a shorter date range and upload it again.")
            self.drain(length)
            return
        data = self.rfile.read(length)
        name = clean_filename(self.headers.get("X-Filename", ""), SAMPLE_FILES[kind])
        table = load_table(data, kind, name)  # raises InputError, so a bad file never replaces a good one
        with self.server.lock:
            self.server.uploads[kind] = (name, data)
        self.send_json(200, {"kind": kind, "filename": name, "rows": len(table.rows)})

    def drain(self, length: int):
        """Read and discard a rejected body so the browser gets to read our answer instead of a reset."""
        remaining = min(length, DRAIN_LIMIT_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def run_uploaded(self):
        with self.server.lock:
            files = dict(self.server.uploads)
        missing = [name for kind, name in SAMPLE_FILES.items() if kind not in files]
        if missing:
            raise InputError(f"Still missing: {', '.join(missing)}. Upload all three files, then run triage.")
        result = triage_bytes(files)
        self.finish_triage(result, {kind: name for kind, (name, _) in files.items()}, "upload")

    def run_sample(self):
        result = triage_dir(self.server.sample_dir)
        self.finish_triage(result, dict(SAMPLE_FILES), "sample")

    def finish_triage(self, result: TriageResult, filenames: dict[str, str], source: str):
        explanations, warnings = explain(self.server, result)
        payload = build_result(result, explanations, filenames, source)
        payload["warnings"] += warnings
        with self.server.lock:
            self.server.last = (result.exceptions, explanations)
        self.send_json(200, payload)

    def send_report(self):
        with self.server.lock:
            last = self.server.last
        if last is None:
            raise InputError("There is no result to download yet. Run triage, or use the sample data, first.")
        body = b"\xef\xbb\xbf" + to_csv(*last).encode("utf-8")  # the BOM makes Excel show the rupee sign properly
        self.send_bytes(200, body, "text/csv; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="exception-report.csv"'})
