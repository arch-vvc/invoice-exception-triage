"""The HTTP API and the dashboard page, against a real server on a free port with a fake explainer."""
import csv
import http.client
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from triage import server as triage_server
from triage.engine import SAMPLE_FILES
from triage.server import make_server

DEMO_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = DEMO_DIR / "sample_data"
INDEX_HTML = DEMO_DIR / "triage" / "web" / "index.html"
KINDS = list(SAMPLE_FILES)


def fake_explainer(excs):
    return {e.id: f"Fake explanation for {e.invoice_ids[0]}." for e in excs}


class Client:
    def __init__(self, server):
        self.host, self.port = server.server_address[:2]
        self.base = f"http://{self.host}:{self.port}"

    def request(self, method, path, body=None, headers=None):
        """Returns (status, headers, body bytes) for any status, including errors."""
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def get_json(self, path):
        status, _, body = self.request("GET", path)
        return status, json.loads(body)

    def upload(self, kind, data, filename=None):
        headers = {"X-Filename": filename or SAMPLE_FILES.get(kind, "x.csv"), "Content-Type": "text/csv"}
        status, _, body = self.request("POST", f"/api/upload/{kind}", data, headers)
        return status, json.loads(body)

    def upload_samples(self):
        for kind, name in SAMPLE_FILES.items():
            status, body = self.upload(kind, (SAMPLE_DIR / name).read_bytes())
            assert status == 200, body

    def triage(self):
        status, _, body = self.request("POST", "/api/triage", b"")
        return status, json.loads(body)


def start(explainer=fake_explainer, sample_dir=SAMPLE_DIR):
    server = make_server("127.0.0.1", 0, sample_dir, explainer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def client():
    server, thread = start()
    yield Client(server)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def answer_key_rows():
    with open(SAMPLE_DIR / "expected_exceptions.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- the page ----

def test_index_is_html(client):
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "Invoice exception triage" in body.decode("utf-8")


def test_server_binds_to_localhost_only(client):
    assert client.host == "127.0.0.1"


def test_page_pulls_in_nothing_from_the_internet():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert not re.search(r"https?://", html)
    assert not re.search(r"<script[^>]+src=", html)
    assert not re.search(r"<link[^>]+href=", html)
    assert "@import" not in html


def test_page_never_writes_data_with_innerhtml():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "innerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "document.write" not in html


def test_page_never_formats_money_itself():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "₹" not in html  # amounts come from the *_display strings
    assert "toLocaleString" not in html
    assert "Intl.NumberFormat" not in html


@pytest.mark.parametrize("path", ["/index.html", "/triage/server.py", "/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd",
                                  "/api", "/nope"])
def test_only_the_dashboard_and_api_are_served(client, path):
    status, _, body = client.request("GET", path)
    assert status == 404
    assert "error" in json.loads(body)


# ---- sample data ----

def test_sample_totals(client):
    status, result = client.get_json("/api/sample")
    assert status == 200
    totals = result["totals"]
    assert totals["invoices_checked"] == 40
    assert totals["exceptions_found"] == len(answer_key_rows()) == len(result["exceptions"])
    assert totals["amount_at_risk"] == pytest.approx(sum(e["amount_at_risk"] for e in result["exceptions"]), abs=0.01)
    assert totals["amount_at_risk_display"].startswith("₹")
    assert result["source"] == "sample"


def test_sample_exceptions_are_sorted_by_risk(client):
    _, result = client.get_json("/api/sample")
    amounts = [e["amount_at_risk"] for e in result["exceptions"]]
    assert amounts == sorted(amounts, reverse=True)
    assert amounts[0] == max(amounts)
    assert result["exceptions"][0]["id"] == "EX-01"


def test_sample_totals_break_down_by_type_and_count_high_severity(client):
    _, result = client.get_json("/api/sample")
    totals, excs = result["totals"], result["exceptions"]
    assert sum(totals["by_type"].values()) == len(excs)
    for kind, count in totals["by_type"].items():
        assert count == sum(1 for e in excs if e["type"] == kind)
    assert totals["high_count"] == sum(1 for e in excs if e["severity"] == "high")
    assert totals["high_count"] > 0


def test_every_exception_has_the_planned_fields(client):
    _, result = client.get_json("/api/sample")
    wanted = {"id", "type", "type_label", "severity", "invoice_no", "vendor", "po_no", "amount_at_risk",
              "amount_at_risk_display", "suggested_action", "explanation", "rows"}
    for exc in result["exceptions"]:
        assert wanted <= set(exc)
        assert exc["severity"] in {"high", "medium", "low"}
        assert exc["amount_at_risk_display"].startswith("₹")
        assert exc["type_label"] and exc["type_label"] != exc["type"]
        assert exc["suggested_action"]
        assert set(exc["rows"]) == set(KINDS)


def test_type_labels_are_human_readable(client):
    _, result = client.get_json("/api/sample")
    labels = {e["type"]: e["type_label"] for e in result["exceptions"]}
    assert labels["duplicate_invoice"] == "Duplicate invoice"
    assert labels["missing_po"] == "Missing PO"
    assert labels["gst_error"] == "GST error"
    assert labels["vendor_name_mismatch"] == "Vendor name mismatch"
    assert labels["amount_mismatch"] == "Amount mismatch"
    assert labels["quantity_mismatch"] == "Quantity mismatch"
    assert labels["partial_payment"] == "Partial payment"
    assert labels["overpayment"] == "Overpayment"


def test_explanations_come_from_the_explainer(client):
    _, result = client.get_json("/api/sample")
    for exc in result["exceptions"]:
        assert exc["explanation"] == f"Fake explanation for {exc['invoice_no']}."


def primary_invoice_row(exc):
    """The invoice the exception is about. A duplicate lists its original first, so skip that one."""
    return next(r for r in exc["rows"]["invoices"] if r["label"] != "Original")


def test_exception_rows_carry_the_raw_cells_under_the_customers_own_headers(client):
    _, result = client.get_json("/api/sample")
    invoice_header = result["files"]["invoices"]["header"]
    assert "Bill Number" in invoice_header and "Party Name" in invoice_header
    assert result["files"]["invoices"]["filename"] == "invoices.csv"
    assert result["files"]["payments"]["header"][:2] == ["Txn Date", "Payment Voucher"]
    for exc in result["exceptions"]:
        primary = primary_invoice_row(exc)
        assert isinstance(primary["row"], int)
        assert list(primary["cells"]) == [h for h in invoice_header if h in primary["cells"]]
        assert primary["cells"]["Bill Number"] == exc["invoice_no"]


def test_exception_rows_point_at_the_physical_line_in_the_file(client):
    _, result = client.get_json("/api/sample")
    lines = (SAMPLE_DIR / "invoices.csv").read_text(encoding="utf-8").splitlines()
    for exc in result["exceptions"]:
        for entry in exc["rows"]["invoices"]:
            assert entry["cells"]["Bill Number"] in lines[entry["row"] - 1]


def test_rows_match_the_answer_key_invoice_rows(client):
    _, result = client.get_json("/api/sample")
    key = {(r["exception_type"], r["invoice_no"]): int(r["invoice_row"]) for r in answer_key_rows()}
    got = {(e["type"], e["invoice_no"]): primary_invoice_row(e)["row"] for e in result["exceptions"]}
    assert got == key


def test_an_overpayment_lists_its_payment_rows(client):
    _, result = client.get_json("/api/sample")
    over = next(e for e in result["exceptions"] if e["type"] == "overpayment")
    assert len(over["rows"]["payments"]) == 2
    assert all("Payment Voucher" in p["cells"] for p in over["rows"]["payments"])


def test_a_missing_po_has_no_po_rows(client):
    _, result = client.get_json("/api/sample")
    missing = next(e for e in result["exceptions"] if e["type"] == "missing_po")
    assert missing["rows"]["purchase_orders"] == []


# ---- explainer failure ----

def test_a_failing_explainer_gives_blank_explanations_and_a_warning():
    def boom(excs):
        raise RuntimeError("model is down")

    server, thread = start(boom)
    try:
        status, result = Client(server).get_json("/api/sample")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 200
    assert all(e["explanation"] == "" for e in result["exceptions"])
    assert any("explanation" in w.lower() for w in result["warnings"])
    assert "Traceback" not in json.dumps(result)


def test_an_explainer_that_skips_some_ids_leaves_those_blank():
    server, thread = start(lambda excs: {excs[0].id: "Only the first."})
    try:
        _, result = Client(server).get_json("/api/sample")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result["exceptions"][0]["explanation"] == "Only the first."
    assert all(e["explanation"] == "" for e in result["exceptions"][1:])


# ---- uploads ----

def test_upload_returns_kind_filename_and_row_count(client):
    expected = {"invoices": 40, "purchase_orders": 30, "payments": 35}
    for kind, name in SAMPLE_FILES.items():
        status, body = client.upload(kind, (SAMPLE_DIR / name).read_bytes(), filename=f"my_{name}")
        assert status == 200
        assert body == {"kind": kind, "filename": f"my_{name}", "rows": expected[kind]}


def test_three_uploads_then_triage_equals_the_sample_run(client):
    _, sample = client.get_json("/api/sample")
    client.upload_samples()
    status, uploaded = client.triage()
    assert status == 200
    assert uploaded["totals"] == sample["totals"]
    assert [(e["id"], e["type"], e["invoice_no"]) for e in uploaded["exceptions"]] == \
           [(e["id"], e["type"], e["invoice_no"]) for e in sample["exceptions"]]
    assert uploaded["source"] == "upload"


def test_uploaded_filename_is_display_only_and_never_a_path(client):
    status, body = client.upload("invoices", (SAMPLE_DIR / "invoices.csv").read_bytes(),
                                 filename="../../../tmp/evil.csv")
    assert status == 200
    assert "/" not in body["filename"] and ".." not in body["filename"]
    assert body["filename"].endswith("evil.csv")


def test_percent_encoded_filenames_are_decoded_for_display(client):
    name = "खरीद invoices (final).csv"
    status, body = client.upload("invoices", (SAMPLE_DIR / "invoices.csv").read_bytes(), filename=quote(name))
    assert status == 200
    assert body["filename"] == name


def test_triage_uses_the_uploaded_filename_in_the_result(client):
    for kind, name in SAMPLE_FILES.items():
        client.upload(kind, (SAMPLE_DIR / name).read_bytes(), filename=f"march_{name}")
    _, result = client.triage()
    assert result["files"]["invoices"]["filename"] == "march_invoices.csv"


def test_a_bad_upload_does_not_replace_a_good_one(client):
    client.upload_samples()
    status, _ = client.upload("invoices", b"junk,not,an,invoice file\n1,2,3,4\n")
    assert status == 400
    status, result = client.triage()
    assert status == 200
    assert result["totals"]["invoices_checked"] == 40


@pytest.mark.parametrize("label,data", [
    ("cp1252", "Date,Bill Number,Party Name\n01/04/2024,INV-1,Caf\xe9 Rupee \xa0Ltd\n".encode("cp1252")),
    ("empty", b""),
    ("blank", b"  \n\n"),
    ("xlsx", b"PK\x03\x04\x14\x00\x06\x00 rest of a zip"),
    ("header only", b"Date,Bill Number,Party Name,GSTIN/UIN,Order No,Billed Qty,Taxable Value,GST %,Invoice Amount\n"),
    ("missing column", b"Date,Bill Number,Party Name\n01/04/2024,INV-1,Acme\n"),
    ("binary", bytes(range(256))),
])
def test_bad_files_give_a_friendly_400_with_no_traceback(client, label, data):
    status, body = client.upload("invoices", data, filename="invoices.csv")
    assert status == 400, label
    message = body["error"]
    assert message and "Traceback" not in message and "File \"" not in message
    assert "invoices.csv" in message


def test_the_cp1252_error_tells_the_user_how_to_fix_it(client):
    status, body = client.upload("payments", "Paid To\n\xa3\n".encode("cp1252"))
    assert status == 400
    assert "UTF-8" in body["error"]


def test_unknown_upload_kind_is_404_json(client):
    status, body = client.upload("receipts", b"a,b\n1,2\n")
    assert status == 404
    assert "error" in body


def test_upload_needs_a_content_length(client):
    conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
    conn.putrequest("POST", "/api/upload/invoices")
    conn.endheaders()
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 411
    assert "error" in body


def test_a_6_mb_upload_is_413_json(client):
    status, _, body = client.request("POST", "/api/upload/invoices", b"x" * (6 * 1024 * 1024),
                                     {"X-Filename": "big.csv"})
    assert status == 413
    message = json.loads(body)["error"]
    assert "5 MB" in message and "Traceback" not in message


def test_a_413_does_not_stop_the_server_serving_the_next_request(client):
    client.request("POST", "/api/upload/invoices", b"x" * (6 * 1024 * 1024))
    status, _ = client.get_json("/api/sample")
    assert status == 200


# ---- triage ----

def test_triage_with_nothing_uploaded_names_all_three_files(client):
    status, result = client.triage()
    assert status == 400
    for name in SAMPLE_FILES.values():
        assert name in result["error"]


def test_triage_with_a_missing_kind_is_400_and_names_it(client):
    for kind in ("invoices", "payments"):
        client.upload(kind, (SAMPLE_DIR / SAMPLE_FILES[kind]).read_bytes())
    status, result = client.triage()
    assert status == 400
    assert "purchase_orders.csv" in result["error"]
    assert "invoices.csv" not in result["error"]
    assert "Traceback" not in result["error"]


def test_a_new_triage_replaces_the_last_result_for_the_report(client):
    client.get_json("/api/sample")
    client.upload_samples()
    client.triage()
    status, _, body = client.request("GET", "/api/report.csv")
    assert status == 200
    assert len(body.decode("utf-8-sig").splitlines()) == 17


# ---- report ----

def test_report_before_any_triage_is_400_json(client):
    status, headers, body = client.request("GET", "/api/report.csv")
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert "triage" in json.loads(body)["error"].lower()


def test_report_csv_after_the_sample_run(client):
    _, sample = client.get_json("/api/sample")
    status, headers, body = client.request("GET", "/api/report.csv")
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    assert 'attachment; filename="exception-report.csv"' in headers["Content-Disposition"]
    assert body.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM so Excel shows the rupee sign correctly
    text = body.decode("utf-8-sig")
    assert text.splitlines()[0] == ("id,type,severity,invoice_no,vendor,po_no,payment_ids,amount_at_risk,"
                                    "suggested_action,explanation,source_rows")
    rows = list(csv.DictReader(text.splitlines()))
    assert len(rows) == sample["totals"]["exceptions_found"]
    assert rows[0]["id"] == "EX-01"
    assert rows[0]["explanation"].startswith("Fake explanation for ")
    assert re.fullmatch(r"\d+\.\d{2}", rows[0]["amount_at_risk"])
    assert re.fullmatch(r"invoices:[\d,]+(; (purchase_orders|payments):[\d,]+)*", rows[0]["source_rows"])


# ---- failures ----

def test_an_unexpected_error_is_a_generic_500_and_the_traceback_goes_to_stderr(client, monkeypatch, capsys):
    def explode(path):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(triage_server, "triage_dir", explode)
    status, result = client.get_json("/api/sample")
    assert status == 500
    assert result["error"] == "Something went wrong on our side. Check the terminal for details."
    assert "secret internal detail" not in json.dumps(result)
    assert "secret internal detail" in capsys.readouterr().err


def test_a_missing_sample_folder_is_a_friendly_400(tmp_path):
    server, thread = start(sample_dir=tmp_path)
    try:
        status, result = Client(server).get_json("/api/sample")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert status == 400
    assert "Traceback" not in result["error"]
    assert "invoices.csv" in result["error"]


def test_post_to_a_get_only_route_is_404_or_405_json(client):
    status, _, body = client.request("POST", "/api/sample", b"")
    assert status in (404, 405)
    assert "error" in json.loads(body)


def test_responses_are_not_cached_or_sniffed(client):
    _, headers, _ = client.request("GET", "/api/sample")
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
