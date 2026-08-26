"""
Catalogue reconciliation: does the provider still offer the model we configured?

These tests exist because of 2026-08-26, when four of five configured remote
routes were dead simultaneously and every probe reported it as an HTTP status.
`x-preview-f-free` returned 401 "Model x-preview-f-free is not supported", which
reads like an auth problem; the truth was that OpenCode Zen had withdrawn the
model. Two NIM ids were simply absent from NIM's catalogue, where no key would
ever have helped.

The invariants that matter here are about *not overclaiming*: an unreadable
catalogue is UNKNOWN, never ABSENT, and ABSENT never disables anything.
"""
import asyncio
import json
import time

import pytest

from control_plane.providers.catalog import (
    CatalogFetcher, CatalogStatus, ProviderCatalog, _extract_ids,
)


def _catalog(*ids, base="https://example.test/v1"):
    return ProviderCatalog(base, time.time(), model_ids=frozenset(ids), http_status=200)


# --------------------------------------------------------------------------
# Three-valued status
# --------------------------------------------------------------------------

def test_a_listed_model_is_listed():
    status, detail = _catalog("a", "b").status_for("a")
    assert status is CatalogStatus.LISTED
    assert "a" in detail


def test_a_listed_model_is_not_promised_to_serve():
    """
    Zen lists `deepseek-v4-flash-free` and answers HTTP 400 "Model is
    unavailable" for it. The listing is evidence, not a guarantee, and the
    detail text has to say so or an operator will read LISTED as "working".
    """
    _, detail = _catalog("deepseek-v4-flash-free").status_for("deepseek-v4-flash-free")
    assert "not a promise" in detail.lower()


def test_a_missing_model_is_absent():
    status, detail = _catalog("a", "b").status_for("x-preview-f-free")
    assert status is CatalogStatus.ABSENT
    assert "NOT in the provider's catalogue" in detail


def test_absence_is_reported_as_evidence_not_proof():
    """
    Ox Alpha served for weeks as an unlisted stealth preview. If ABSENT were
    stated as proof, the honest thing to do with it would be to disable the
    route — and that would have switched off a working primary.
    """
    _, detail = _catalog("a").status_for("some-preview")
    assert "evidence, not" in detail.lower()


def test_an_unreadable_catalogue_is_unknown_never_absent():
    """
    The single most damaging possible bug in this module: reporting a network
    failure as "the model is gone" and retiring a healthy route.
    """
    unreadable = ProviderCatalog("https://example.test/v1", time.time(),
                                 model_ids=None, error="timeout after 20s")
    status, detail = unreadable.status_for("anything")
    assert status is CatalogStatus.UNKNOWN
    assert "timeout" in detail
    assert unreadable.available is False


def test_an_empty_catalogue_body_is_unreadable_rather_than_empty():
    """
    A response we cannot parse must not become an empty model set, because an
    empty set makes every configured model read as ABSENT at once.
    """
    assert _extract_ids({"unexpected": "shape"}) is None
    assert _extract_ids("nonsense") is None


# --------------------------------------------------------------------------
# Payload shapes providers actually return
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"object": "list", "data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
    ({"models": [{"id": "a"}]}, ["a"]),
    ([{"id": "a"}, "b"], ["a", "b"]),
    ({"data": [{"name": "a"}]}, ["a"]),
    ({"data": []}, []),
])
def test_model_ids_are_extracted_from_the_shapes_providers_return(payload, expected):
    assert _extract_ids(payload) == expected


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------

def test_a_missing_id_suggests_near_misses_from_the_catalogue():
    """
    The operator asked for `nemotron-3-ultra-free` on NIM and NIM offers
    `nvidia/nemotron-3-ultra-550b-a55b`. Handing them that id is the difference
    between a five-minute fix and an afternoon.
    """
    cat = _catalog(
        "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/nemotron-3-super-120b-a12b",
        "openai/gpt-oss-120b",
    )
    hits = cat.suggestions("nemotron-3-ultra-free")
    assert hits, "no suggestion offered"
    assert hits[0] == "nvidia/nemotron-3-ultra-550b-a55b"


def test_suggestions_are_empty_when_nothing_resembles_the_id():
    assert _catalog("openai/gpt-oss-120b").suggestions("zzz") == []


def test_suggestions_are_empty_for_an_unreadable_catalogue():
    unreadable = ProviderCatalog("https://example.test/v1", time.time(), error="boom")
    assert unreadable.suggestions("anything") == []


# --------------------------------------------------------------------------
# Fetching and caching
# --------------------------------------------------------------------------

def test_one_request_per_api_base_not_per_profile(monkeypatch):
    """
    Six profiles across two providers must cost two catalogue requests. Anything
    else makes reconciliation too expensive to run on every doctor pass, which
    is the same as not having it.
    """
    calls = []

    async def fake_get(url, headers, timeout):
        calls.append(url)
        return 200, json.dumps({"data": [{"id": "a"}]})

    f = CatalogFetcher()
    monkeypatch.setattr(f, "_get", fake_get)

    async def run():
        return await asyncio.gather(*[
            f.get("https://one.test/v1") for _ in range(4)
        ] + [f.get("https://two.test/v1") for _ in range(2)])

    results = asyncio.run(run())
    assert len(calls) == 2, calls
    assert all(r.available for r in results)


def test_an_expired_cache_entry_is_refetched(monkeypatch):
    calls = []

    async def fake_get(url, headers, timeout):
        calls.append(url)
        return 200, json.dumps({"data": [{"id": "a"}]})

    f = CatalogFetcher(ttl_s=0.0)
    monkeypatch.setattr(f, "_get", fake_get)

    async def run():
        await f.get("https://one.test/v1")
        await f.get("https://one.test/v1")

    asyncio.run(run())
    assert len(calls) == 2


def test_a_non_200_listing_is_unreadable_not_empty(monkeypatch):
    async def fake_get(url, headers, timeout):
        return 403, "forbidden"

    f = CatalogFetcher()
    monkeypatch.setattr(f, "_get", fake_get)
    cat = asyncio.run(f.get("https://one.test/v1"))
    assert cat.available is False
    assert cat.status_for("a")[0] is CatalogStatus.UNKNOWN
    assert "403" in cat.error


def test_a_transport_failure_is_unreadable_not_empty(monkeypatch):
    async def fake_get(url, headers, timeout):
        raise ConnectionError("no route to host")

    f = CatalogFetcher()
    monkeypatch.setattr(f, "_get", fake_get)
    cat = asyncio.run(f.get("https://one.test/v1"))
    assert cat.available is False
    assert cat.status_for("a")[0] is CatalogStatus.UNKNOWN


def test_the_credential_is_sent_when_one_is_configured(monkeypatch):
    """Some providers list only for authenticated callers."""
    seen = {}

    async def fake_get(url, headers, timeout):
        seen.update(headers)
        return 200, json.dumps({"data": []})

    monkeypatch.setenv("SOME_PROVIDER_KEY", "secret-value")
    f = CatalogFetcher()
    monkeypatch.setattr(f, "_get", fake_get)
    asyncio.run(f.get("https://one.test/v1", "SOME_PROVIDER_KEY"))
    assert seen.get("Authorization") == "Bearer secret-value"
