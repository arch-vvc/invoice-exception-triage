"""The "Show sample data" view: the three sample files as they sit on disk, from the server and the static export."""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_DIR))

from triage.engine import SAMPLE_FILES  # noqa: E402
from triage.sample_view import MAX_LINES, sample_view  # noqa: E402
from triage.server import make_server  # noqa: E402

SAMPLE_DIR = DEMO_DIR / "sample_data"
INDEX_HTML = DEMO_DIR / "triage" / "web" / "index.html"


def test_every_sample_file_comes_through_line_for_line_with_its_heading_lines():
    view = sample_view(SAMPLE_DIR)["files"]
    assert set(view) == set(SAMPLE_FILES)
    invoices = view["invoices"]
    assert invoices["filename"] == "invoices.csv" and invoices["total"] <= MAX_LINES
    assert len(invoices["rows"]) == invoices["total"] == 45  # the file has 45 lines of text
    assert invoices["rows"][0][0].startswith("SYNTHETIC DEMO DATA")  # nothing is cleaned away
    assert invoices["width"] == max(len(row) for row in invoices["rows"])


def test_cells_keep_indian_number_grouping_and_the_rupee_sign():
    flat = [cell for row in sample_view(SAMPLE_DIR)["files"]["invoices"]["rows"] for cell in row]
    assert "₹1,45,140" in flat and "1,13,575.00" in flat


def test_a_long_file_is_cut_at_the_limit_and_says_how_long_it_really_is(tmp_path):
    for name in SAMPLE_FILES.values():
        (tmp_path / name).write_text("a,b\n" * (MAX_LINES + 20), encoding="utf-8")
    invoices = sample_view(tmp_path)["files"]["invoices"]
    assert len(invoices["rows"]) == MAX_LINES and invoices["total"] == MAX_LINES + 20


@pytest.fixture(scope="module")
def base_url():
    server = make_server("127.0.0.1", 0, SAMPLE_DIR, lambda excs: {})
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def test_the_server_sends_the_sample_files(base_url):
    with urllib.request.urlopen(base_url + "/api/sample-files", timeout=10) as resp:
        assert resp.status == 200 and resp.headers["Content-Type"].startswith("application/json")
        assert json.loads(resp.read()) == sample_view(SAMPLE_DIR)


def test_the_sample_files_address_only_answers_get(base_url):
    request = urllib.request.Request(base_url + "/api/sample-files", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 405


def test_the_page_has_the_button_and_never_writes_cells_as_html():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="show-sample"' in html and "Show sample data" in html and "/api/sample-files" in html
    assert "innerHTML" not in html


def test_the_vercel_entry_point_sends_the_same_sample_files():
    import importlib.util

    spec = importlib.util.spec_from_file_location("vercel_index", DEMO_DIR / "api" / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}
    environ = {"REQUEST_METHOD": "GET", "QUERY_STRING": "p=api/sample-files"}
    body = b"".join(module.app(environ, lambda status, headers: seen.update(status=status)))
    assert seen["status"].startswith("200") and json.loads(body) == sample_view(SAMPLE_DIR)
