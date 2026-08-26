"""
Provider catalogue reconciliation.

The failure this exists to catch: a model id in our default routing table stops
being served, and every probe reports a generic failure that reads like a
transient outage. On 2026-08-26 four of the five configured remote routes were
dead at once, and the doctor's output for each was an HTTP status and a
provider message — true, but it never said the word that mattered, which is
that the model is no longer in the provider's catalogue.

So we fetch `GET {api_base}/models` and reconcile. Both providers we route to
serve that listing **without a credential**, which is what makes this cheap
enough to run on every doctor pass.

Two asymmetries are load-bearing here, and both were observed in this repo
rather than assumed:

  * **Listed does not imply served.** OpenCode Zen lists `deepseek-v4-flash-free`
    and then answers `Model is unavailable` for it.
  * **Absent does not imply unserved.** `x-preview-f-free` (Ox Alpha) was a
    stealth preview: served for weeks while never appearing in the listing.

Therefore the catalogue result is *evidence recorded alongside* the live probe,
never a gate in front of it. The live request remains the authority on whether a
route works. What reconciliation buys is a diagnosis — `absent from the
provider's catalogue` — instead of an operator guessing whether their key
expired.

A catalogue we could not fetch is `UNKNOWN`, never `ABSENT`. Reporting a network
failure as "the model is gone" would be exactly the fabrication the rest of this
codebase refuses to make.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class CatalogStatus(str, Enum):
    LISTED = "listed"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCatalog:
    """One provider's model listing at one moment, or the reason we lack it."""

    api_base: str
    fetched_at: float
    model_ids: Optional[FrozenSet[str]] = None
    http_status: Optional[int] = None
    error: str = ""

    @property
    def available(self) -> bool:
        return self.model_ids is not None

    def status_for(self, model: str) -> Tuple[CatalogStatus, str]:
        if self.model_ids is None:
            return CatalogStatus.UNKNOWN, (
                f"catalogue not readable ({self.error or 'no reason recorded'}); "
                f"cannot say whether '{model}' is still offered"
            )
        if model in self.model_ids:
            return CatalogStatus.LISTED, (
                f"'{model}' is in the provider's catalogue "
                f"({len(self.model_ids)} models listed) — note that being listed "
                f"is not a promise it will serve"
            )
        return CatalogStatus.ABSENT, (
            f"'{model}' is NOT in the provider's catalogue "
            f"({len(self.model_ids)} models listed). The id may have been "
            f"renamed, retired, or was never public. This is evidence, not "
            f"proof: unlisted preview models have served before."
        )

    def suggestions(self, model: str, limit: int = 5) -> list:
        """
        Catalogue ids that look like near misses for a missing one.

        Deliberately dumb — shared prefix or shared distinctive token. The point
        is to hand the operator `nvidia/nemotron-3-ultra-550b-a55b` when they
        asked for `nemotron-3-ultra-free`, not to guess for them.
        """
        if not self.model_ids:
            return []
        wanted = {t for t in _tokens(model) if len(t) > 2}
        if not wanted:
            return []
        scored = []
        for cid in self.model_ids:
            overlap = wanted & set(_tokens(cid))
            if overlap:
                scored.append((len(overlap), -len(cid), cid))
        scored.sort(reverse=True)
        return [cid for _, _, cid in scored[:limit]]


def _tokens(model_id: str) -> list:
    out, cur = [], []
    for ch in model_id.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


class CatalogFetcher:
    """
    Fetches and caches `/models` per API base.

    Cached per instance and per base so a doctor run over six profiles across
    two providers makes two catalogue requests, not six.
    """

    def __init__(self, timeout_s: float = 20.0, ttl_s: float = 300.0) -> None:
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        self._cache: Dict[str, ProviderCatalog] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def cached(self, api_base: str) -> Optional[ProviderCatalog]:
        cat = self._cache.get(api_base.rstrip("/"))
        if cat is None:
            return None
        if time.time() - cat.fetched_at > self.ttl_s:
            return None
        return cat

    async def get(self, api_base: str, secret_ref: Optional[str] = None) -> ProviderCatalog:
        base = api_base.rstrip("/")
        hit = self.cached(base)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(base, asyncio.Lock())
        async with lock:
            hit = self.cached(base)
            if hit is not None:
                return hit
            cat = await self._fetch(base, secret_ref)
            self._cache[base] = cat
            return cat

    async def _fetch(self, base: str, secret_ref: Optional[str]) -> ProviderCatalog:
        headers = {"Accept": "application/json"}
        key = os.environ.get(secret_ref) if secret_ref else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{base}/models"
        try:
            status, text = await self._get(url, headers, self.timeout_s)
        except asyncio.TimeoutError:
            return ProviderCatalog(base, time.time(),
                                   error=f"timeout after {self.timeout_s}s")
        except Exception as exc:
            return ProviderCatalog(base, time.time(),
                                   error=f"{type(exc).__name__}: {exc}"[:200])

        if status != 200:
            return ProviderCatalog(
                base, time.time(), http_status=status,
                error=f"HTTP {status}: {(text or '')[:160]}",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ProviderCatalog(base, time.time(), http_status=status,
                                   error="200 but the body was not JSON")

        ids = _extract_ids(payload)
        if ids is None:
            return ProviderCatalog(
                base, time.time(), http_status=status,
                error="200 but no recognisable model list in the body",
            )
        return ProviderCatalog(base, time.time(), model_ids=frozenset(ids),
                               http_status=status)

    @staticmethod
    async def _get(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        """
        httpx first, urllib only as a fallback — the same ordering, and for the
        same reason, as the chat probe: urllib draws a Cloudflare 1010 from Zen
        that httpx does not. See `doctor.ProviderDoctor._post`.
        """
        try:
            import httpx
        except ImportError:
            httpx = None  # type: ignore[assignment]

        if httpx is not None:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=timeout)
                return r.status_code, r.text

        import urllib.error
        import urllib.request

        def _do() -> Tuple[int, str]:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 5)


def _extract_ids(payload: object) -> Optional[list]:
    """
    Pull model ids out of the shapes providers actually return.

    OpenAI-compatible is `{"data": [{"id": ...}]}`; a few return a bare list or
    nest under "models". Anything else returns None so the caller reports the
    catalogue as unreadable rather than as empty — an empty catalogue would make
    every model read as ABSENT, which is the worst possible wrong answer here.
    """
    if isinstance(payload, dict):
        for key in ("data", "models"):
            inner = payload.get(key)
            if isinstance(inner, list):
                payload = inner
                break
        else:
            return None
    if not isinstance(payload, list):
        return None
    ids = []
    for item in payload:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if isinstance(mid, str):
                ids.append(mid)
        elif isinstance(item, str):
            ids.append(item)
    return ids
