"""Vercel entry point for the exception triage demo. A stateless WSGI wrapper around the real engine.

The local server keeps uploaded files in memory between requests, which a serverless host cannot do, so the
online demo runs the bundled sample data only: GET /api/sample and the CSV download both triage the sample
files again (fast, and the same every time). Uploads are refused with a message that points to the repo.
Nothing is stored on the server and no model is ever called.
"""
import functools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from triage.engine import SAMPLE_FILES, triage_dir  # noqa: E402
from triage.explain import explain_all  # noqa: E402
from triage.report import to_csv_bytes  # noqa: E402
from triage.server import CONTENT_SECURITY_POLICY, INDEX_HTML, build_result  # noqa: E402

SAMPLE_DIR = ROOT / "sample_data"
explainer = functools.partial(explain_all, backend="offline")
UPLOADS_OFF = ("Uploads are switched off in the online demo, which runs the sample data only. "
               "Clone the repo to triage your own Tally or Zoho Books exports: "
               "github.com/arch-vvc/invoice-exception-triage")
REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 501: "Not Implemented"}


def respond(start_response, status: int, body: bytes, content_type: str, extra=()):
    headers = [("Content-Type", content_type), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
               ("X-Content-Type-Options", "nosniff"), *extra]
    start_response(f"{status} {REASONS.get(status, 'Error')}", headers)
    return [body]


def reply_json(start_response, status: int, payload):
    return respond(start_response, status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")


def sample_result():
    result = triage_dir(SAMPLE_DIR)
    return result, explainer(result.exceptions)


def app(environ, start_response):
    method = environ["REQUEST_METHOD"]
    path = environ.get("PATH_INFO", "/").split("?")[0]
    if path in ("/", "/index.html") and method == "GET":
        return respond(start_response, 200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8",
                       [("Content-Security-Policy", CONTENT_SECURITY_POLICY)])
    if path == "/api/sample" and method == "GET":
        result, found = sample_result()
        explanations = {e.id: str(found.get(e.id) or "") for e in result.exceptions}
        return reply_json(start_response, 200, build_result(result, explanations, dict(SAMPLE_FILES), "sample"))
    if path == "/api/report.csv" and method == "GET":
        result, found = sample_result()
        return respond(start_response, 200, to_csv_bytes(result.exceptions, found), "text/csv; charset=utf-8",
                       [("Content-Disposition", 'attachment; filename="exception-report.csv"')])
    if path.startswith("/api/upload/") or path == "/api/triage":
        return reply_json(start_response, 501, {"error": UPLOADS_OFF})
    return reply_json(start_response, 404, {"error": "Not found."})
