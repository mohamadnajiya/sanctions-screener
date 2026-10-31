from __future__ import annotations

import time

from agentkit import testing
from screen.tools import CATALOGUE

EX = {
    "screen_name": {
        "name": "Saddam Hussein Al-Tikriti",
        "dob": "1937",
        "nationality": "IQ",
        "entity_type": "individual",
    },
    "screen_batch": {
        "subjects": [
            {"name": "Ali Kony", "reference": "A"},
            {"name": "ali kony", "reference": "B"},
            {"name": "John Smith"},
        ]
    },
    "audit_report": {
        "subjects": [{"name": "Qusay Saddam Hussein", "dob": "1966"}, {"name": "Jane Doe"}],
        "case_reference": "KYC-1",
    },
    "list_status": {},
    "explain_match": {"name_a": "Abd al-Rahman Al Shaikh", "name_b": "عبدالرحمن الشيخ"},
}


def test_mcp_matches_catalogue():
    testing.check_mcp_matches_catalogue(CATALOGUE)


def test_rest_matches_catalogue():
    testing.check_rest_matches_catalogue(CATALOGUE)


def test_parity():
    ex = {k: v for k, v in EX.items() if k != "audit_report"}  # the report carries a timestamp
    testing.check_parity(CATALOGUE, ex)


def test_no_request_logging(caplog):
    testing.check_no_request_logging(
        CATALOGUE, "screen_name", {"name": "Zorblax Quintrell Secretmarker77"}, "Secretmarker77", caplog
    )
    testing.check_no_request_logging(
        CATALOGUE,
        "screen_batch",
        {"subjects": [{"name": "Zorblax Secretmarker78", "reference": "REF-SECRET-78"}]},
        "Secretmarker78",
        caplog,
    )


def test_error_shapes():
    testing.check_error_shapes(CATALOGUE, "screen_name", {"name": ""})
    testing.check_error_shapes(CATALOGUE, "screen_name", {"name": "x", "dob": "13/01/1990"})
    testing.check_error_shapes(CATALOGUE, "screen_name", {"name": "x", "nationality": "UAE"})
    testing.check_error_shapes(CATALOGUE, "screen_name", {"name": "x", "lists": ["opensanctions"]})
    testing.check_error_shapes(CATALOGUE, "screen_batch", {"subjects": []})
    testing.check_error_shapes(CATALOGUE, "explain_match", {"name_a": "x"})


def test_p95_screen_name_under_50ms_full_corpus():
    CATALOGUE.call("screen_name", EX["screen_name"])  # warm the index
    # a deliberately common two-part name: thousands of candidate aliases share 'muhammad'
    p95 = testing.check_p95_latency(CATALOGUE, "screen_name", {"name": "Mohamed Ali Hussein"}, budget_ms=50, n=200)
    p95b = testing.check_p95_latency(CATALOGUE, "screen_name", {"name": "محمد علي حسين"}, budget_ms=50, n=100)
    print(f"\np95 screen_name latin {p95:.1f} ms, arabic {p95b:.1f} ms")


def test_batch_dedupes_and_charges_per_unique_name():
    env = CATALOGUE.call("screen_batch", EX["screen_batch"])
    r = env.result
    assert r["count"] == 2 and r["duplicates_removed"] == 1 and r["billable_names"] == 2
    assert CATALOGUE.count_for("screen_batch", env) == 2
    kony = next(x for x in r["results"] if x["name"] == "Ali Kony")
    assert sorted(kony["references"]) == ["A", "B"] and kony["decision_hint"] == "likely_match"
    assert r["summary"] == {"clear": 1, "review": 0, "likely_match": 1}


def test_billing_and_envelope():
    ev = {t.name: t.event for t in CATALOGUE.tools}
    assert ev == {
        "screen_name": "name-screened",
        "screen_batch": "name-screened",
        "audit_report": "audit-report",
        "list_status": None,
        "explain_match": None,
    }
    env = CATALOGUE.call("screen_name", EX["screen_name"])
    assert CATALOGUE.count_for("screen_name", env) == 1
    assert env.disclaimer and "not a legal determination" in env.disclaimer
    assert env.data_version and env.sources and all(s.startswith("http") for s in env.sources)
    assert env.data_confidence in ("high", "medium")
    env2 = CATALOGUE.call("audit_report", EX["audit_report"])
    assert CATALOGUE.count_for("audit_report", env2) == 1


def test_audit_report_html_is_self_contained():
    env = CATALOGUE.call("audit_report", EX["audit_report"])
    html = env.result["html"]
    assert html.startswith("<!doctype html>") and "Qusay Saddam Hussein" in html and "No match" in html
    assert "src=" not in html and "href=" not in html  # no external resources
    assert "SHA-256" in html and env.data_version in html and "Disclaimer" in html
    assert env.result["summary"] == {"clear": 1, "review": 0, "likely_match": 1}


def test_list_status_reports_every_list():
    env = CATALOGUE.call("list_status", {})
    lists = {x["list"]: x for x in env.result["lists"]}
    assert set(lists) == {"ofac_sdn", "ofac_cons", "un", "eu", "uk", "uae"}
    for x in lists.values():
        assert (
            x["entries"] > 0
            and x["sha256"]
            and x["source_url"].startswith("https://")
            and x["days_since_fetch"] is not None
        )
    assert env.result["total_entries"] > 30_000


def test_load_time_is_reasonable():
    from screen.matcher import Index

    t0 = time.perf_counter()
    Index()
    assert time.perf_counter() - t0 < 20
