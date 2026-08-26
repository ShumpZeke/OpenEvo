"""
Terminal dashboard rendering.

Thin presentation, tested for one thing that is not cosmetic: a section with no
data must be absent, not a table of zeros. The no-fake-data rule applies to the
terminal exactly as it does to the browser.
"""

from __future__ import annotations

import re

import pytest

from oe_max import dashboard


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture
def fake_get(monkeypatch):
    """Replace the dashboard's HTTP fetch with a scripted map of responses."""
    responses = {}

    def _get(url, timeout=5.0):
        for key, value in responses.items():
            if key in url:
                return value
        return None

    monkeypatch.setattr(dashboard, "_get", _get)
    return responses


def test_no_attempts_renders_no_section_at_all(fake_get):
    fake_get["route-quality"] = {"routes": {}, "coverage": {}, "comparison": {}}
    assert dashboard._route_quality_lines("http://cp", "run_1") == []


def test_an_unreachable_control_plane_is_silent_rather_than_zeroed(fake_get):
    """A failed fetch must not render a route table full of zeros."""
    assert dashboard._route_quality_lines("http://cp", "run_1") == []


def test_a_route_renders_its_measured_numbers(fake_get):
    fake_get["route-quality"] = {
        "routes": {"opencode_zen/x-preview-f-free": {
            "route": "opencode_zen/x-preview-f-free", "attempts": 12,
            "failures": 3, "duplicates": 1, "validity_rate": 0.5,
            "improvement_rate": 0.25, "mean_latency_s": 284.0,
            "improvement_per_request": 0.0123,
        }},
        "coverage": {"note": "1 of 13 candidates are unattributed"},
        "comparison": {"verdict": "insufficient evidence"},
    }
    text = plain("\n".join(dashboard._route_quality_lines("http://cp", "run_1")))
    assert "opencode_zen/x-preview-f-free" in text
    assert "284" in text and "50%" in text and "0.0123" in text
    assert "insufficient evidence" in text
    assert "1 of 13 candidates are unattributed" in text


def test_routes_are_ordered_by_evidence(fake_get):
    """The route with the most attempts is the one worth reading first."""
    def route(name, n):
        return {"route": name, "attempts": n, "failures": 0, "duplicates": 0,
                "validity_rate": 1.0, "improvement_rate": 0.0,
                "mean_latency_s": 10.0, "improvement_per_request": 0.0}

    fake_get["route-quality"] = {
        "routes": {"a/thin": route("a/thin", 2), "b/thick": route("b/thick", 40)},
        "coverage": {}, "comparison": {},
    }
    text = plain("\n".join(dashboard._route_quality_lines("http://cp", "run_1")))
    assert text.index("b/thick") < text.index("a/thin")


def test_a_missing_latency_renders_as_absent(fake_get):
    """No measurement is a dash, never a zero that reads as "instant"."""
    fake_get["route-quality"] = {
        "routes": {"a/b": {"route": "a/b", "attempts": 1, "failures": 0,
                           "duplicates": 0, "validity_rate": 0.0,
                           "improvement_rate": 0.0, "mean_latency_s": None,
                           "improvement_per_request": 0.0}},
        "coverage": {}, "comparison": {},
    }
    text = plain("\n".join(dashboard._route_quality_lines("http://cp", "run_1")))
    assert "-" in text.split("\n")[-1]


def test_yield_shows_raw_and_distinct_together(fake_get):
    """
    Raw yield alone reads as a win it is not: the local N=3 run showed 2.42 raw
    against 0.75 distinct, and the difference was the same program again.
    """
    fake_get["route-quality"] = {
        "routes": {"a/b": {"route": "a/b", "attempts": 12, "failures": 0,
                           "duplicates": 0, "validity_rate": 1.0,
                           "improvement_rate": 0.5, "mean_latency_s": 84.0,
                           "improvement_per_request": 0.2}},
        "throughput": {"candidates_per_request": 2.42,
                       "useful_candidates_per_request": 0.75,
                       "duplicate_share": 0.69, "extra_offspring": 17},
        "coverage": {}, "comparison": {},
    }
    text = plain("\n".join(dashboard._route_quality_lines("http://cp", "run_1")))
    assert "2.42/req" in text and "0.75/req" in text
    assert "69%" in text
    assert "17" in text


def test_a_run_without_throughput_data_shows_no_yield_line(fake_get):
    fake_get["route-quality"] = {
        "routes": {"a/b": {"route": "a/b", "attempts": 1, "failures": 0,
                           "duplicates": 0, "validity_rate": 1.0,
                           "improvement_rate": 0.0, "mean_latency_s": 1.0,
                           "improvement_per_request": 0.0}},
        "coverage": {}, "comparison": {},
    }
    text = plain("\n".join(dashboard._route_quality_lines("http://cp", "run_1")))
    assert "yield" not in text
