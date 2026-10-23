"""Explanations: offline templates, model backends (always faked here), validation and the cache file."""
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from triage import explain
from triage.engine import TriageException, triage_dir
from triage.explain import (
    explain_all,
    fingerprint,
    is_valid_sentence,
    offline_sentence,
    refresh_cache,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
COMMITTED_CACHE = SAMPLE_DIR / "explanations_cache.json"


@pytest.fixture(scope="module")
def sample_excs():
    return triage_dir(SAMPLE_DIR).exceptions


def make_exc(exc_id="EX-01", kind="partial_payment", **facts) -> TriageException:
    base = {
        "invoice_no": "INV-9", "vendor": "Test Traders", "po_no": "PO-1",
        "total": 100000.0, "total_display": "₹1,00,000",
        "paid": 60000.0, "paid_display": "₹60,000",
        "tds": 0.0, "tds_display": "₹0",
        "outstanding": 40000.0, "outstanding_display": "₹40,000",
    }
    return TriageException(
        id=exc_id, type=kind, severity="low", invoice_ids=["INV-9"], po_ids=["PO-1"], payment_ids=[],
        source_rows={"invoices": [5], "purchase_orders": [], "payments": []},
        amount_at_risk=40000.0, suggested_action="x", facts=base | facts,
    )


GOOD = "Invoice INV-9 from Test Traders is for ₹1,00,000 but only ₹60,000 was paid, leaving ₹40,000 outstanding."


def no_subprocess(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("subprocess must not run")
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)


# ---- offline templates ----

def test_every_type_on_sample_data_has_an_offline_sentence(sample_excs):
    assert {e.type for e in sample_excs} == set(explain.TEMPLATES)
    for exc in sample_excs:
        text = offline_sentence(exc)
        assert exc.facts["invoice_no"] in text, text
        assert text.endswith("."), text
        assert "\n" not in text and len(text) <= 300, text
        assert is_valid_sentence(text, exc.facts, exc.amount_at_risk), text


@pytest.mark.parametrize("reason", ["missing", "format", "checksum", "slab", "rate_vs_po", "amount"])
def test_gst_template_varies_by_reason(reason):
    exc = make_exc(kind="gst_error", reason=reason, gstin="27AAAAA0000A1Z5", gst_rate=18.0, po_gst_rate=12.0)
    text = offline_sentence(exc)
    assert "INV-9" in text and text.endswith(".")
    others = {offline_sentence(make_exc(kind="gst_error", reason=r, gstin="27AAAAA0000A1Z5", gst_rate=18.0,
                                        po_gst_rate=12.0)) for r in explain.GST_REASONS if r != reason}
    assert text not in others


def test_duplicate_sentence_reads_well(sample_excs):
    dup = next(e for e in sample_excs if e.type == "duplicate_invoice" and e.facts["invoice_no"] == "INV/42")
    assert offline_sentence(dup) == (
        "Invoice INV/42 from Sundaram Brake Linings Pvt Ltd repeats INV-0042 for ₹1,45,140, "
        "so paying both would pay the vendor twice."
    )


def test_offline_never_spawns_a_subprocess(monkeypatch, sample_excs, tmp_path):
    no_subprocess(monkeypatch)
    out = explain_all(sample_excs, backend="offline", cache_path=tmp_path / "c.json")
    assert set(out) == {e.id for e in sample_excs}
    assert not (tmp_path / "c.json").exists()


# ---- fingerprint ----

def test_fingerprint_is_stable_and_ignores_id():
    a, b = make_exc("EX-01"), make_exc("EX-07")
    assert fingerprint(a) == fingerprint(b)
    assert len(fingerprint(a)) == 16
    assert fingerprint(a) != fingerprint(make_exc(invoice_no="INV-10"))
    assert fingerprint(a) != fingerprint(make_exc(kind="overpayment"))


def test_fingerprint_stable_across_runs(sample_excs):
    again = triage_dir(SAMPLE_DIR).exceptions
    assert [fingerprint(e) for e in sample_excs] == [fingerprint(e) for e in again]


# ---- validation ----

def test_validation_rules():
    facts = make_exc().facts
    assert is_valid_sentence(GOOD, facts)
    assert not is_valid_sentence("", facts)
    assert not is_valid_sentence("Invoice INV-9 is short paid", facts), "must end with a full stop"
    assert not is_valid_sentence("Invoice is short paid.", facts), "must name the invoice"
    assert not is_valid_sentence("Invoice INV-9 is short.\nIt is bad.", facts), "one line only"
    assert not is_valid_sentence("Invoice INV-9 " + "x" * 300 + ".", facts), "too long"


def test_invented_rupee_amount_is_rejected():
    facts = make_exc().facts
    assert not is_valid_sentence("Invoice INV-9 is short by ₹41,000.", facts)
    assert not is_valid_sentence("Invoice INV-9 is short by Rs 41000.", facts)
    assert not is_valid_sentence("Invoice INV-9 is short by Rs. 39,999.50.", facts)
    assert is_valid_sentence("Invoice INV-9 is short by ₹40,000, then ₹60,000 was paid.", facts)


# ---- backends via an injected runner ----

def test_runner_sentence_is_used_and_cached(tmp_path):
    cache = tmp_path / "c.json"
    exc = make_exc()
    out = explain_all([exc], backend="claude-cli", cache_path=cache, runner=lambda prompt: GOOD + "\n")
    assert out == {"EX-01": GOOD}
    entry = json.loads(cache.read_text(encoding="utf-8"))["entries"][fingerprint(exc)]
    assert entry == {"text": GOOD, "backend": "claude-cli", "type": "partial_payment"}


def test_cache_hit_skips_backend(tmp_path):
    cache = tmp_path / "c.json"
    exc = make_exc()
    explain_all([exc], backend="claude-cli", cache_path=cache, runner=lambda p: GOOD)

    def boom(prompt):
        raise AssertionError("backend must not be called on a cache hit")

    assert explain_all([make_exc("EX-09")], backend="claude-cli", cache_path=cache, runner=boom) == {"EX-09": GOOD}
    assert explain_all([exc], backend="offline", cache_path=cache) == {"EX-01": GOOD}


def test_timeout_exception_and_garbage_fall_back_to_template(tmp_path):
    exc = make_exc()
    template = offline_sentence(exc)

    def timeout(prompt):
        raise subprocess.TimeoutExpired("claude", 60)

    def broken(prompt):
        raise RuntimeError("no network")

    garbage = (lambda p: "", lambda p: "I cannot help with that", lambda p: "line one.\nline two.")
    for runner in (timeout, broken, *garbage):
        cache = tmp_path / "c.json"
        assert explain_all([exc], backend="claude-cli", cache_path=cache, runner=runner) == {"EX-01": template}
        assert not cache.exists(), "fallbacks are never cached"


def test_invented_amount_from_model_falls_back(tmp_path):
    exc = make_exc()
    bad = "Invoice INV-9 from Test Traders is short by ₹41,000."
    out = explain_all([exc], backend="claude-cli", cache_path=tmp_path / "c.json", runner=lambda p: bad)
    assert out["EX-01"] == offline_sentence(exc)


def test_prompt_uses_type_and_facts_only():
    seen = []
    explain_all([make_exc()], backend="claude-cli", cache_path=Path("/nonexistent/dir/c.json"),
                runner=lambda p: seen.append(p) or GOOD)
    assert len(seen) == 1
    prompt = seen[0]
    assert "INV-9" in prompt and "partial_payment" in prompt and "₹40,000" in prompt
    assert "under 40 words" in prompt and "one sentence" in prompt
    assert "EX-01" not in prompt


def test_real_backends_fall_back_when_unavailable(monkeypatch, tmp_path, capsys):
    no_subprocess(monkeypatch)
    monkeypatch.setattr(explain.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exc = make_exc()
    for backend in ("claude-cli", "anthropic"):
        assert explain_all([exc], backend=backend, cache_path=tmp_path / "c.json") == {"EX-01": offline_sentence(exc)}
    err = capsys.readouterr().err
    assert "claude" in err and "ANTHROPIC_API_KEY" in err


def test_claude_cli_runner_command(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=GOOD + "\n", stderr="")

    monkeypatch.setattr(explain.subprocess, "run", fake_run)
    assert explain.run_claude_cli("hello") == GOOD
    argv, kwargs = calls[0]
    assert argv == ["claude", "-p", "--model", "haiku"]
    assert kwargs["input"] == "hello" and kwargs["timeout"] == 60 and not kwargs.get("shell")
    assert kwargs["cwd"] == tempfile.gettempdir(), "must not load this repo's CLAUDE.md and hooks"


def test_anthropic_request_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": GOOD}]}).encode()

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data),
                    timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(explain.urllib.request, "urlopen", fake_urlopen)
    assert explain.run_anthropic("hello") == GOOD
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["Anthropic-version"] == "2023-06-01"
    assert seen["body"]["model"] == "claude-haiku-5-5" and seen["body"]["max_tokens"] == 120
    assert seen["timeout"] == 30


# ---- cache file ----

def test_refresh_prunes_stale_entries_and_regenerates(tmp_path):
    cache = tmp_path / "c.json"
    stale = {"text": "Invoice OLD-1 is stale.", "backend": "claude-cli", "type": "overpayment"}
    old = {"text": "Invoice INV-9 from Test Traders is short paid.", "backend": "claude-cli", "type": "partial_payment"}
    cache.write_text(json.dumps({"version": 1, "entries": {"deadbeefdeadbeef": stale, fingerprint(make_exc()): old}}),
                     encoding="utf-8")
    out = explain_all([make_exc()], backend="claude-cli", cache_path=cache, refresh=True, runner=lambda p: GOOD)
    assert out == {"EX-01": GOOD}
    entries = json.loads(cache.read_text(encoding="utf-8"))["entries"]
    assert list(entries) == [fingerprint(make_exc())]
    assert entries[fingerprint(make_exc())]["text"] == GOOD


def test_refresh_keeps_previous_sentence_when_model_fails(tmp_path):
    cache = tmp_path / "c.json"
    explain_all([make_exc()], backend="claude-cli", cache_path=cache, runner=lambda p: GOOD)
    out = explain_all([make_exc()], backend="claude-cli", cache_path=cache, refresh=True, runner=lambda p: "")
    assert out == {"EX-01": GOOD}


def test_cache_file_format_is_sorted_and_unescaped(tmp_path):
    cache = tmp_path / "c.json"
    excs = [make_exc("EX-01"), make_exc("EX-02", invoice_no="INV-10")]
    explain_all(excs, backend="claude-cli", cache_path=cache,
                runner=lambda p: GOOD.replace("INV-9", "INV-10") if "INV-10" in p else GOOD)
    raw = cache.read_text(encoding="utf-8")
    assert "₹1,00,000" in raw and "\\u20b9" not in raw
    data = json.loads(raw)
    assert data["version"] == 1 and list(data["entries"]) == sorted(data["entries"])
    assert raw == json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"version": 1, "entries": []}',
                                     '{"version": 1, "entries": {"abc": "just text"}}'])
def test_corrupt_cache_is_treated_as_empty(tmp_path, content):
    cache = tmp_path / "c.json"
    cache.write_text(content, encoding="utf-8")
    exc = make_exc()
    assert explain_all([exc], backend="offline", cache_path=cache) == {"EX-01": offline_sentence(exc)}
    assert explain_all([exc], backend="claude-cli", cache_path=cache, runner=lambda p: GOOD) == {"EX-01": GOOD}
    assert json.loads(cache.read_text(encoding="utf-8"))["entries"][fingerprint(exc)]["text"] == GOOD


def test_missing_cache_file_is_fine(tmp_path):
    exc = make_exc()
    assert explain_all([exc], backend="offline", cache_path=tmp_path / "nope" / "c.json") == {
        "EX-01": offline_sentence(exc)}


def test_refresh_cache_hook_returns_ok_count_and_total(tmp_path):
    cache = tmp_path / "c.json"
    excs = [make_exc("EX-01"), make_exc("EX-02", invoice_no="INV-10")]
    runner = lambda p: GOOD if '"invoice_no": "INV-9"' in p else "garbage"  # noqa: E731
    assert refresh_cache(excs, "claude-cli", cache, runner=runner) == (1, 2)
    assert explain.cached_count(excs, cache) == 1
    assert refresh_cache(excs, "offline", cache) == (1, 2), "offline refresh must not wipe the cache"


# ---- committed cache ----

def test_cache_entries_are_grounded(sample_excs):
    entries = explain.load_cache(COMMITTED_CACHE)
    by_fp = {fingerprint(e): e for e in sample_excs}
    stray = set(entries) - set(by_fp)
    assert not stray, f"cache entries that match no current sample exception: {sorted(stray)}"
    for fp, entry in entries.items():
        exc = by_fp[fp]
        assert entry["type"] == exc.type
        assert is_valid_sentence(entry["text"], exc.facts, exc.amount_at_risk), f"{exc.id}: {entry['text']}"


def test_committed_cache_file_is_valid_json():
    data = json.loads(COMMITTED_CACHE.read_text(encoding="utf-8"))
    assert data["version"] == 1 and isinstance(data["entries"], dict)


# ---- fix round 1 ----

def rate_exc(**changes) -> TriageException:
    facts = {"qty": 500.0, "invoice_rate": 12.5, "invoice_rate_display": "₹13", "po_rate": 12.0,
             "po_rate_display": "₹12", "diff": 250.0, "diff_display": "₹250"} | changes
    return make_exc(kind="amount_mismatch", **facts)


def test_fractional_rates_are_shown_to_two_decimals():
    exc = rate_exc()
    text = offline_sentence(exc)
    assert "₹12.50 a unit against ₹12 on PO-1" in text and "₹13" not in text
    assert is_valid_sentence(text, exc.facts, exc.amount_at_risk)
    assert not is_valid_sentence(text.replace("₹12.50", "₹13"), exc.facts, exc.amount_at_risk)
    assert "₹12.50" in explain.build_prompt(exc)


def test_rates_use_indian_grouping():
    exc = rate_exc(invoice_rate=12345.5, po_rate=12000.0, po_rate_display="₹12,000")
    assert "₹12,345.50 a unit against ₹12,000 on" in offline_sentence(exc)


def test_numbers_use_indian_grouping_not_scientific():
    assert explain._num(1600.0) == "1,600"
    assert explain._num(1234567.0) == "12,34,567"
    assert explain._num(12.5) == "12.5"
    assert explain._num(1234567.25) == "12,34,567.25"
    assert explain._num(18.0) == "18" and explain._num(0.25) == "0.25"
    big = make_exc(kind="quantity_mismatch", invoiced_qty=1234567.0, po_qty=1200000.0, excess_qty=34567.0)
    assert "12,34,567 units against 12,00,000 ordered" in offline_sentence(big)


def test_missing_po_wording(sample_excs):
    no_ref = next(e for e in sample_excs if e.facts["invoice_no"] == "AS/0071")
    assert offline_sentence(no_ref).startswith(
        "Invoice AS/0071 from Apex Safety Products quotes no PO and none matches, "
        "so there is no approved order behind this ₹")
    bad_ref = next(e for e in sample_excs if e.facts["invoice_no"] == "OSE/2024/361")
    text = offline_sentence(bad_ref)
    assert "quotes PO-00999, which does not exist in the PO file" in text and "bill." in text


def test_rate_vs_po_sentence_states_rupee_impact(sample_excs):
    exc = next(e for e in sample_excs if e.facts.get("reason") == "rate_vs_po")
    text = offline_sentence(exc)
    assert explain.format_inr(exc.amount_at_risk) in text
    assert is_valid_sentence(text, exc.facts, exc.amount_at_risk)


def test_prompt_explains_what_each_type_means(sample_excs):
    ref = make_exc(kind="missing_po", po_ref_given="PO-00999")
    blank = make_exc(kind="missing_po", po_ref_given="")
    assert "no such PO exists in the PO file" in explain.build_prompt(ref)
    assert "quotes no PO and none could be matched" in explain.build_prompt(blank)
    for exc in sample_excs:
        prompt = explain.build_prompt(exc)
        assert f'starting with "Invoice {exc.facts["invoice_no"]}"' in prompt
        assert explain.meaning(exc) in prompt
    checksum = next(e for e in sample_excs if e.facts.get("reason") == "checksum")
    assert "check digit" in explain.meaning(checksum)


def test_validation_rejects_markdown_and_partial_invoice_numbers():
    facts = make_exc().facts
    assert not is_valid_sentence("Invoice INV-9 has **₹40,000** outstanding.", facts)
    assert not is_valid_sentence("Invoice INV-9 has `₹40,000` outstanding.", facts)
    assert not is_valid_sentence("# Invoice INV-9 has ₹40,000 outstanding.", facts)
    assert not is_valid_sentence("Invoice INV-90 has ₹40,000 outstanding.", facts)
    assert not is_valid_sentence("Invoice XINV-9 has ₹40,000 outstanding.", facts)
    assert is_valid_sentence("Invoice INV-9, from Test Traders, has ₹40,000 outstanding.", facts)


def test_validation_compares_amounts_numerically():
    facts = make_exc().facts
    assert is_valid_sentence("Invoice INV-9 is for ₹1,00,000.00 in total.", facts)
    assert is_valid_sentence("Invoice INV-9 is for Rs 100000 in total.", facts)
    assert not is_valid_sentence("Invoice INV-9 is for ₹1,00,001 in total.", facts)


def test_validation_rejects_bare_numbers_followed_by_rupees():
    facts = make_exc().facts
    assert is_valid_sentence("Invoice INV-9 is short by 40,000 rupees.", facts)
    assert not is_valid_sentence("Invoice INV-9 is short by 41,000 rupees.", facts)
    assert not is_valid_sentence("Invoice INV-9 is short by 41000 rupee.", facts)


def test_amount_at_risk_is_an_allowed_amount():
    facts = {"invoice_no": "AS/0071", "vendor": "Apex", "po_no": "", "po_ref_given": ""}
    text = "Invoice AS/0071 is a ₹1,29,800 bill."
    assert not is_valid_sentence(text, facts)
    assert is_valid_sentence(text, facts, 129800.0)


def test_refresh_drops_cached_entries_that_fail_validation(tmp_path):
    cache = tmp_path / "c.json"
    exc = make_exc()
    bad = {"text": "Invoice INV-9 is short by ₹41,000.", "backend": "claude-cli", "type": "partial_payment"}
    cache.write_text(json.dumps({"version": 1, "entries": {fingerprint(exc): bad}}), encoding="utf-8")
    out = explain_all([exc], backend="claude-cli", cache_path=cache, refresh=True, runner=lambda p: "")
    assert out == {"EX-01": offline_sentence(exc)}
    assert json.loads(cache.read_text(encoding="utf-8"))["entries"] == {}


def test_cache_is_written_through_a_temp_file_in_the_same_folder(monkeypatch, tmp_path):
    seen = []
    real_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(explain.tempfile, "mkstemp", lambda **kw: seen.append(kw) or real_mkstemp(**kw))
    explain_all([make_exc()], backend="claude-cli", cache_path=tmp_path / "c.json", runner=lambda p: GOOD)
    assert [Path(kw["dir"]) for kw in seen] == [tmp_path]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json"]
