"""run.py end to end, in subprocesses. Nothing here can reach a model: the server is always offline."""
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).resolve().parents[1]
RUN_PY = DEMO_DIR / "run.py"


def run(*args, timeout=30):
    return subprocess.run([sys.executable, str(RUN_PY), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def wait_for_server(base: str, proc: subprocess.Popen, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"run.py exited early with code {proc.returncode}")
        try:
            if get(base + "/")[0] == 200:
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"the server did not answer within {seconds} seconds")


def test_check_prints_the_ac5_lines_and_exits_zero():
    started = time.monotonic()
    result = run("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert time.monotonic() - started < 5
    lines = result.stdout.splitlines()
    assert "invoices checked: 40" in lines
    found = next(line for line in lines if line.startswith("exceptions found: "))
    count = int(found.split(": ")[1])
    assert count == 16
    assert any(re.fullmatch(r"amount at risk: ₹[\d,]+", line) for line in lines)
    assert any(re.fullmatch(rf"explanations cached: \d+/{count}", line) for line in lines)
    assert f"answer key: {count}/{count} matched, 0 extra" in lines


def test_compare_with_key_reports_missing_and_extra():
    import run as run_module  # run.py sits in the demo folder, which conftest puts on sys.path

    key = {("duplicate_invoice", "INV/42"), ("made_up_type", "X-1")}
    found = {("duplicate_invoice", "INV/42"), ("missing_po", "AS/0071")}
    missing, extra = run_module.compare_with_key(found, key)
    assert missing == {("made_up_type", "X-1")}
    assert extra == {("missing_po", "AS/0071")}


def test_refresh_without_explainer_is_refused_with_a_hint():
    result = run("--refresh")
    assert result.returncode != 0
    assert "--explainer" in result.stderr
    assert "Traceback" not in result.stderr


def test_refresh_with_the_offline_explainer_is_refused():
    result = run("--refresh", "--explainer", "offline")
    assert result.returncode != 0
    assert "--explainer" in result.stderr
    assert "Traceback" not in result.stderr


def test_server_serves_the_dashboard_and_sample_then_stops_cleanly():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, str(RUN_PY), "--port", str(port)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        wait_for_server(base, proc)
        status, body = get(base + "/api/sample")
        assert status == 200
        payload = json.loads(body)
        assert payload["totals"]["invoices_checked"] == 40
        assert all(e["explanation"] for e in payload["exceptions"])
    finally:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail("run.py did not stop within 5 seconds of SIGTERM")
    assert f"Exception triage dashboard: {base}" in out
    assert "Traceback" not in err


def test_a_busy_port_gets_a_friendly_message():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        result = run("--port", str(port))
    assert result.returncode == 1
    assert "--port" in result.stderr
    assert "Traceback" not in result.stderr
