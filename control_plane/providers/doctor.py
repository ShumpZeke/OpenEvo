"""
Provider doctor.

Replaces assumptions with measurements. For each enabled profile it probes what
actually happens right now — reachability, auth, latency, whether the model
answers a trivial completion, whether it accepts a `tools` array, and how it
behaves when rate limited.

This exists because everything the routing policy depends on is documented as
changeable, and on 2026-08-26 all of it changed at once: four of the five
configured remote routes were dead in the same afternoon — Ox Alpha withdrawn
from OpenCode Zen entirely, and both NVIDIA NIM model ids absent from NIM's
catalogue. A router built on the docs alone is wrong within a week. A router
built on probes self-corrects.

Nothing here fabricates a result. A probe that cannot run (no credential, no
network, a saturated free pool) is reported as SKIPPED with the reason, never
as a pass.

Three lessons from that afternoon are now encoded as probes rather than as
prose:

  * **Reconcile against the catalogue.** A withdrawn model used to surface as a
    bare HTTP status. Now `catalog` says which of the provider's listed models
    it is, or is not. See `catalog.py` for why that is evidence and not a gate.
  * **A tools probe must see a tool call.** Any 200 carrying `choices` used to
    count as tool support verified. `nemotron-3-ultra-free` answers 200 with an
    empty message when its budget runs out, so the old probe recorded tool
    support this repo had not observed.
  * **Give the probe room to answer.** These are reasoning models; a 16-token
    budget is spent on hidden reasoning before the first visible token, and the
    reply comes back empty. An empty 200 is now diagnosed as truncation instead
    of passing as a healthy completion.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..telemetry.events import Component, Event, EventType, Status
from ..telemetry.bus import emit
from .catalog import CatalogFetcher, CatalogStatus, ProviderCatalog
from .profiles import Capability, FreeStatus, ModelProfile


class ProbeResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass
class Probe:
    name: str
    result: ProbeResult
    detail: str = ""
    latency_ms: Optional[float] = None
    http_status: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "result": self.result.value, "detail": self.detail,
            "latency_ms": self.latency_ms, "http_status": self.http_status,
        }


@dataclass
class ProviderReport:
    profile_id: str
    provider: str
    model: str
    api_base: str
    checked_at: float = field(default_factory=time.time)
    available: bool = False
    probes: List[Probe] = field(default_factory=list)
    verified_capabilities: List[Capability] = field(default_factory=list)
    free_status: FreeStatus = FreeStatus.UNKNOWN
    free_note: str = ""
    latency_ms: Optional[float] = None
    preferred: bool = False
    summary: str = ""
    # Which capabilities a probe actually ran for, pass or fail. Distinct from
    # `verified_capabilities`, which holds only the ones that passed.
    #
    # Without the distinction, `apply_reports` cannot tell "tools were probed
    # and did not work" from "tools were never probed", and it overwrote the
    # profile with whatever passed. Running the doctor with --no-tools therefore
    # erased TOOLS from every profile and left every agent role with no route —
    # while the exclusion reason read "verified by provider doctor", which was
    # false. An unrun probe must never be recorded as a result.
    probed_capabilities: List[Capability] = field(default_factory=list)
    catalog_status: CatalogStatus = CatalogStatus.UNKNOWN
    catalog_detail: str = ""
    catalog_suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "checked_at": self.checked_at,
            "available": self.available,
            "probes": [p.to_dict() for p in self.probes],
            "verified_capabilities": [c.value for c in self.verified_capabilities],
            "probed_capabilities": [c.value for c in self.probed_capabilities],
            "free_status": self.free_status.value,
            "free_note": self.free_note,
            "latency_ms": self.latency_ms,
            "preferred": self.preferred,
            "summary": self.summary,
            "catalog_status": self.catalog_status.value,
            "catalog_detail": self.catalog_detail,
            "catalog_suggestions": list(self.catalog_suggestions),
        }


# Probe token budgets. These are not arbitrary.
#
# The old value was 16, chosen when the routes were plain completion models. It
# survived into a table of reasoning models, where the entire budget is spent on
# hidden reasoning and the visible reply is empty: `nemotron-3-ultra-free`
# answered a tools probe with HTTP 200, `finish_reason: null`, no message
# content and no tool call, and the doctor recorded tool support as verified.
#
# 512/1024 is enough for every route measured on 2026-08-26 to emit a visible
# answer, while still being a cheap probe. Truncation is now diagnosed rather
# than passed (see `_read_completion`), so if a future model needs more the
# report says so in words instead of quietly lying.
CHAT_PROBE_MAX_TOKENS = 512
TOOLS_PROBE_MAX_TOKENS = 1024


class ProviderDoctor:
    def __init__(
        self,
        timeout_s: float = 25.0,
        catalog_fetcher: Optional[CatalogFetcher] = None,
        reconcile_catalog: bool = True,
        tools_probe_attempts: int = 2,
    ) -> None:
        self.timeout_s = timeout_s
        # A capability that works one time in three is not a capability you can
        # route agent work to, and a single probe of one cannot tell the
        # difference. `laguna-s-2.1-free` emitted a tool call on 1 of 3 attempts
        # on 2026-08-26, failing the other two with the same "Endpoint is
        # unavailable" that Ox Alpha's tool bug used to produce. Probed once, it
        # promotes itself into every agent role a third of the time.
        #
        # So TOOLS is verified only when every attempt emits a tool call, and
        # the detail records the ratio either way. Two attempts is the cheapest
        # count that catches an intermittent route; raise it where the cost of a
        # wrong promotion is higher than the cost of the probe.
        self.tools_probe_attempts = max(1, tools_probe_attempts)
        # One fetcher across the whole run: six profiles on two providers cost
        # two catalogue requests, not six.
        self.catalog = catalog_fetcher or CatalogFetcher(timeout_s=timeout_s)
        self.reconcile_catalog = reconcile_catalog

    async def check_all(
        self, profiles: List[ModelProfile], probe_tools: bool = True
    ) -> List[ProviderReport]:
        reports = await asyncio.gather(
            *(self.check(p, probe_tools=probe_tools) for p in profiles if p.enabled),
            return_exceptions=True,
        )
        out: List[ProviderReport] = []
        for prof, rep in zip([p for p in profiles if p.enabled], reports):
            if isinstance(rep, BaseException):
                out.append(
                    ProviderReport(
                        profile_id=prof.id, provider=prof.provider, model=prof.model,
                        api_base=prof.api_base, available=False,
                        summary=f"doctor error: {type(rep).__name__}: {rep}",
                        probes=[Probe("doctor", ProbeResult.FAIL, str(rep))],
                    )
                )
            else:
                out.append(rep)
        return out

    async def check(self, profile: ModelProfile, probe_tools: bool = True) -> ProviderReport:
        rep = ProviderReport(
            profile_id=profile.id, provider=profile.provider,
            model=profile.model, api_base=profile.api_base,
            free_status=profile.free_status, free_note=profile.free_note,
        )

        # 1. Catalogue reconciliation. Cheap — both providers we route to serve
        #    `/models` without a credential — and it runs FIRST, ahead of the
        #    credential check, precisely because it does not need one. That
        #    ordering is the whole point: an uncredentialled NIM route used to
        #    report "NVIDIA_API_KEY not set" and stop, which is true and
        #    useless. Both configured NIM model ids were simultaneously absent
        #    from NIM's catalogue, and no amount of key would have fixed that.
        #
        #    It never gates the live probe. A model absent from the listing can
        #    still serve (Ox Alpha did, for weeks, as an unlisted preview) and a
        #    listed one can still refuse (Zen lists `deepseek-v4-flash-free` and
        #    answers "Model is unavailable"). The live request remains the
        #    authority; this is the diagnosis attached to its verdict.
        if self.reconcile_catalog:
            rep.probes.append(await self._probe_catalog(profile, rep))

        # 2. Credential presence — a local endpoint legitimately needs none.
        if profile.secret_ref and profile.has_secret():
            rep.probes.append(Probe("credential", ProbeResult.PASS,
                                    f"{profile.secret_ref} present"))
        elif profile.secret_ref and not getattr(profile, "requires_key", True):
            # Keyless-capable route: probe it anyway. Zen serves Ox Alpha with
            # no Authorization header, and skipping here would report a working
            # primary route as unverified.
            rep.probes.append(Probe(
                "credential", ProbeResult.SKIPPED,
                f"{profile.secret_ref} not set; this route is documented as "
                f"usable without one, so probing anyway",
            ))
        elif profile.secret_ref:
            rep.probes.append(Probe(
                "credential", ProbeResult.SKIPPED,
                f"{profile.secret_ref} not set — live probes cannot run. "
                f"This is reported as unverified, not as failure.",
            ))
            # Say the more important thing first when we know it. "Credential
            # missing" sends the operator to look for a key; if the model id is
            # not in the provider's catalogue, no key was ever going to help.
            if rep.catalog_status is CatalogStatus.ABSENT:
                rep.summary = (
                    f"not in the provider's catalogue, and {profile.secret_ref} "
                    f"is not set — fix the model id first"
                )
            else:
                rep.summary = "credential missing; not verified"
            rep.available = False
            self._emit(rep)
            return rep
        else:
            rep.probes.append(Probe("credential", ProbeResult.SKIPPED, "no credential required"))

        # 3. Plain chat completion.
        chat = await self._probe_chat(profile, with_tools=False)
        rep.probes.append(chat)
        rep.latency_ms = chat.latency_ms
        if chat.result is not ProbeResult.SKIPPED:
            rep.probed_capabilities.append(Capability.CHAT)
        if chat.result is ProbeResult.PASS:
            rep.available = True
            rep.verified_capabilities.append(Capability.CHAT)

        # 4. Tool calling — the probe that matters most for agent roles.
        #    PASS here requires an actual `tool_calls` entry in the reply, not
        #    merely that the endpoint tolerated a `tools` array. See
        #    `_read_completion`.
        if probe_tools and rep.available:
            tools = await self._probe_tools(profile)
            rep.probes.append(tools)
            if tools.result is not ProbeResult.SKIPPED:
                rep.probed_capabilities.append(Capability.TOOLS)
            if tools.result is ProbeResult.PASS:
                rep.verified_capabilities.append(Capability.TOOLS)

        # 5. Free status. We can confirm "not free" from a billing signal, but
        #    we never upgrade to "free forever" — the docs say limited time.
        if rep.available and profile.free_status is FreeStatus.FREE_LIMITED_TIME:
            rep.free_note = (
                profile.free_note
                or "Free for a limited time per provider docs; verify before relying on it."
            )

        rep.summary = self._summarize(rep)
        self._emit(rep)
        return rep

    async def _probe_catalog(self, profile: ModelProfile, rep: ProviderReport) -> Probe:
        """
        Ask the provider what it currently offers, and say where this id stands.

        Records the verdict on the report so the Models page and the summary can
        both use it. Never fails the profile on its own — see the note at the
        call site, and the module docstring in `catalog.py`.
        """
        cat: ProviderCatalog = await self.catalog.get(profile.api_base, profile.secret_ref)
        status, detail = cat.status_for(profile.model)
        rep.catalog_status = status
        rep.catalog_detail = detail

        if status is CatalogStatus.LISTED:
            return Probe("catalog", ProbeResult.PASS, detail, http_status=cat.http_status)
        if status is CatalogStatus.UNKNOWN:
            # Could not read the listing. Inconclusive, never "gone".
            return Probe("catalog", ProbeResult.SKIPPED, detail, http_status=cat.http_status)

        rep.catalog_suggestions = cat.suggestions(profile.model)
        if rep.catalog_suggestions:
            detail += " Closest ids currently offered: " + ", ".join(
                rep.catalog_suggestions
            ) + "."
            rep.catalog_detail = detail
        return Probe("catalog", ProbeResult.FAIL, detail, http_status=cat.http_status)

    async def _probe_tools(self, profile: ModelProfile) -> Probe:
        """
        Verify tool calling over several attempts, and require all of them.

        Attempts run in sequence rather than concurrently: two simultaneous
        requests can trip a per-route concurrency limit and fail for a reason
        that has nothing to do with tool support.
        """
        attempts = [
            await self._probe_chat(profile, with_tools=True)
            for _ in range(self.tools_probe_attempts)
        ]
        passed = [a for a in attempts if a.result is ProbeResult.PASS]
        skipped = [a for a in attempts if a.result is ProbeResult.SKIPPED]
        latencies = [a.latency_ms for a in attempts if a.latency_ms is not None]
        latency = min(latencies) if latencies else None
        n = len(attempts)

        if len(passed) == n:
            return Probe("tools", ProbeResult.PASS,
                         f"{n}/{n} attempts emitted a tool call — {passed[0].detail}",
                         latency, passed[0].http_status)
        if not passed and len(skipped) == n:
            # Every attempt was inconclusive (rate limited, bot-blocked). Do not
            # convert that into a capability failure.
            return Probe("tools", ProbeResult.SKIPPED,
                         f"could not verify over {n} attempts — {skipped[0].detail}",
                         latency, skipped[0].http_status)
        if passed:
            others = [a for a in attempts if a.result is not ProbeResult.PASS]
            return Probe(
                "tools", ProbeResult.FAIL,
                f"intermittent: only {len(passed)}/{n} attempts emitted a tool "
                f"call. Not routed to agent roles — a capability that works "
                f"sometimes fails the run that needed it. Failure seen: "
                f"{others[0].detail}",
                latency, others[0].http_status,
            )
        failed = [a for a in attempts if a.result is ProbeResult.FAIL]
        return Probe("tools", ProbeResult.FAIL,
                     f"0/{n} attempts emitted a tool call — {failed[0].detail}",
                     latency, failed[0].http_status)

    async def _probe_chat(self, profile: ModelProfile, with_tools: bool) -> Probe:
        """Issue one real minimal request against the endpoint."""
        name = "chat_with_tools" if with_tools else "chat"
        key = os.environ.get(profile.secret_ref) if profile.secret_ref else None
        url = profile.api_base.rstrip("/") + "/chat/completions"
        # The tools prompt has to actually *want* the tool. A "reply with ok"
        # prompt carrying a `tools` array proves the endpoint tolerates the
        # field and nothing more; the model has no reason to call anything, so a
        # well-behaved model and a broken one produce the same reply.
        body: Dict[str, Any] = {
            "model": profile.model,
            "messages": [{
                "role": "user",
                "content": (
                    "What is the weather in Paris? Call the get_weather function."
                    if with_tools else "Reply with the single word: ok"
                ),
            }],
            "max_tokens": TOOLS_PROBE_MAX_TOKENS if with_tools else CHAT_PROBE_MAX_TOKENS,
            "temperature": 0,
        }
        if with_tools:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"},
                        },
                        "required": ["city"],
                    },
                },
            }]

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        t0 = time.perf_counter()
        try:
            status, text = await self._post(url, headers, body, self.timeout_s)
        except asyncio.TimeoutError:
            return Probe(name, ProbeResult.FAIL,
                         f"timeout after {self.timeout_s}s", None, None)
        except Exception as exc:
            return Probe(name, ProbeResult.FAIL, f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - t0) * 1000.0

        if status == 200:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return Probe(name, ProbeResult.FAIL, "200 but body was not JSON",
                             latency, status)
            result, detail = _read_completion(data, with_tools)
            return Probe(name, result, detail, latency, status)
        if status == 429:
            # A saturated shared free pool is not a broken model, and calling it
            # one would retire a working route. Zen answers `FreeUsageLimitError`
            # for `mimo-v2.5-free` and `big-pickle` under anonymous load while
            # both remain in its catalogue. Inconclusive, on the same principle
            # as the Cloudflare 1010 case below.
            return Probe(name, ProbeResult.SKIPPED,
                         "rate limited (429) — the route exists but would not "
                         "serve this probe; inconclusive, not a capability "
                         f"failure. Provider said: {(text or '')[:160]}",
                         latency, status)
        if status == 403 and "1010" in (text or ""):
            # Cloudflare client-fingerprint block, not an auth or provider
            # failure. Reported as inconclusive: blaming the provider for our
            # own client's TLS/UA signature would be actively misleading.
            return Probe(name, ProbeResult.SKIPPED,
                         "blocked by the provider's bot protection "
                         "(Cloudflare 1010) — inconclusive, not a provider fault",
                         latency, status)
        if status in (401, 403):
            return Probe(name, ProbeResult.FAIL,
                         f"auth rejected ({status})", latency, status)
        # Surface the provider's own message: this is how issue #44300 shows up.
        return Probe(name, ProbeResult.FAIL,
                     f"HTTP {status}: {text[:300]}", latency, status)

    @staticmethod
    async def _post(url: str, headers: Dict[str, str], body: Dict[str, Any],
                    timeout: float) -> tuple:
        """
        POST via httpx, falling back to urllib only if httpx is unavailable.

        This ordering is not a style preference. Probing OpenCode Zen with
        `urllib` returns **HTTP 403 `error code: 1010`** for every model — a
        Cloudflare client-fingerprint block — while `httpx` and `curl` return
        200 against the same endpoint in the same minute. With urllib first,
        this doctor reported a perfectly healthy Ox Alpha as unavailable.

        The urllib path is kept as a last resort so the doctor still runs in a
        bare environment, but a 1010 from it is reported as an inconclusive
        transport block rather than as a provider failure — blaming the provider
        for our own client's fingerprint would be worse than admitting we could
        not tell.
        """
        try:
            import httpx
        except ImportError:
            httpx = None  # type: ignore[assignment]

        if httpx is not None:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=body, headers=headers, timeout=timeout)
                return r.status_code, r.text

        import urllib.error
        import urllib.request

        def _do() -> tuple:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=timeout + 5)

    @staticmethod
    def _summarize(rep: ProviderReport) -> str:
        if not rep.available:
            # Lead with the catalogue when the id is gone. "HTTP 401: Model
            # x-preview-f-free is not supported" reads like an auth problem and
            # sends the operator hunting for a key that would not have helped.
            if rep.catalog_status is CatalogStatus.ABSENT:
                extra = (
                    " Closest listed: " + ", ".join(rep.catalog_suggestions[:3])
                    if rep.catalog_suggestions else ""
                )
                return (
                    f"model '{rep.model}' is no longer in {rep.provider}'s "
                    f"catalogue.{extra}"
                )
            failed = [p for p in rep.probes if p.result is ProbeResult.FAIL]
            if failed:
                return failed[0].detail
            skipped = [p for p in rep.probes
                       if p.result is ProbeResult.SKIPPED and p.http_status == 429]
            if skipped:
                return "rate limited; could not verify"
            return "unavailable"
        caps = ", ".join(c.value for c in rep.verified_capabilities) or "none"
        out = f"available ({caps})" + (f", {rep.latency_ms:.0f}ms" if rep.latency_ms else "")
        if rep.catalog_status is CatalogStatus.ABSENT:
            # It serves but is unlisted — exactly Ox Alpha's old shape. Worth
            # saying out loud, because an unlisted route can be withdrawn
            # without any deprecation notice.
            out += " — but unlisted in the provider's catalogue, so treat it as a preview"
        return out

    @staticmethod
    def _emit(rep: ProviderReport) -> None:
        emit(Event(
            type=EventType.SYSTEM_HEALTH,
            component=Component.PROVIDER,
            status=Status.OK if rep.available else Status.WARNING,
            summary=f"provider doctor: {rep.profile_id} — {rep.summary}",
            metrics={"latency_ms": rep.latency_ms or 0.0,
                     "available": 1.0 if rep.available else 0.0},
            metadata={"report": rep.to_dict(), "provider": rep.provider,
                      "model": rep.model},
        ))


def _read_completion(data: Dict[str, Any], with_tools: bool) -> tuple:
    """
    Decide what a 200 actually proved, and say so.

    The old rule was `bool(data.get("choices"))`. Every case below returned a
    pass under it, and two of them are not passes:

      * a tools probe whose reply contains no `tool_calls` — the endpoint
        accepted the array and the model never called anything. Observed on
        `nemotron-3-ultra-free`: HTTP 200, `finish_reason: null`, empty message,
        no tool call, recorded as tool support verified.
      * a completion truncated before its first visible token, which is what a
        reasoning model does when the probe's budget is too small. Diagnosing it
        as truncation points at the token budget; passing it hides a route that
        will produce nothing useful in a real run.

    Returns (ProbeResult, detail).
    """
    choices = data.get("choices")
    if not choices:
        return ProbeResult.FAIL, "200 but the response carried no choices"

    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    usage = data.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    if with_tools:
        if tool_calls:
            names = ", ".join(
                (tc.get("function") or {}).get("name", "?")
                for tc in tool_calls if isinstance(tc, dict)
            )
            return ProbeResult.PASS, (
                f"model emitted {len(tool_calls)} tool call(s): {names}"
            )
        # No tool call. Distinguish "ran out of room" from "declined".
        if finish == "length" or (not content and not finish):
            hint = (
                f" ({reasoning} tokens went to hidden reasoning)"
                if reasoning else ""
            )
            return ProbeResult.FAIL, (
                f"200, but the reply was truncated before any tool call"
                f"{hint}. Raise the probe budget above "
                f"{TOOLS_PROBE_MAX_TOKENS} for this model, or treat it as "
                f"unable to complete a tool call within a usable budget."
            )
        return ProbeResult.FAIL, (
            f"200 and the endpoint accepted the `tools` array, but the model "
            f"emitted no tool call (finish_reason={finish!r}). Accepting the "
            f"field is not the same as supporting it."
        )

    if content.strip():
        return ProbeResult.PASS, "completion returned"
    if finish == "length" or reasoning:
        hint = f" — {reasoning} tokens went to hidden reasoning" if reasoning else ""
        return ProbeResult.FAIL, (
            f"200 but the completion was empty, truncated at "
            f"finish_reason={finish!r}{hint}. The budget was spent before the "
            f"first visible token."
        )
    if tool_calls:
        # Answering a plain prompt with a tool call is odd but is still a live,
        # well-formed completion; do not fail the route over it.
        return ProbeResult.PASS, "completion returned (as a tool call)"
    return ProbeResult.FAIL, (
        f"200 but the message was empty (finish_reason={finish!r})"
    )


def apply_reports(profiles: List[ModelProfile], reports: List[ProviderReport]) -> None:
    """
    Fold doctor findings back into the profiles the router reads.

    This is what makes the system self-correcting: if Ox Alpha's tool support is
    fixed upstream, the next doctor run records TOOLS as verified and the router
    can promote it into agent roles with no code change.
    """
    by_id = {r.profile_id: r for r in reports}
    for p in profiles:
        rep = by_id.get(p.id)
        if rep is None:
            continue
        # Merge, never replace. Only capabilities the doctor actually probed
        # are updated; anything unprobed keeps whatever the profile already
        # held. Replacing wholesale meant a run with `probe_tools=False` recorded
        # "tools verified absent" for every route and emptied every agent role.
        if rep.probed_capabilities:
            known = list(p.verified_capabilities
                         if p.verified_capabilities is not None
                         else p.declared_capabilities)
            for cap in rep.probed_capabilities:
                passed = cap in rep.verified_capabilities
                if passed and cap not in known:
                    known.append(cap)
                elif not passed and cap in known:
                    known.remove(cap)
            p.verified_capabilities = known
        if rep.free_note:
            p.free_note = rep.free_note
        # Catalogue status is recorded whether or not the live probe ran, so an
        # uncredentialled route still tells the operator that its model id is
        # stale. It does not disable anything — see `catalog.py`.
        p.catalog_status = rep.catalog_status.value
        p.catalog_detail = rep.catalog_detail
        p.catalog_suggestions = list(rep.catalog_suggestions)

        # Record the live verdict, including a negative one. Until this existed,
        # a doctor run could establish that a route was returning HTTP 503 and
        # change nothing about routing: the profile kept its declared
        # capabilities, kept leading its chain, and the circuit breaker had to
        # rediscover the outage with real requests.
        #
        # A probe that could not run leaves the verdict alone. "No credential"
        # and "rate limited" are not evidence about the route, and recording
        # them as a failure would suppress a model nobody has shown to be
        # broken. That distinction is the whole reason ProbeResult has a
        # SKIPPED value.
        live = [
            pr for pr in rep.probes
            if pr.name in ("chat", "chat_with_tools", "tools")
            and pr.result is not ProbeResult.SKIPPED
        ]
        if live:
            p.last_probe_ok = rep.available
            p.last_probe_at = rep.checked_at
            p.last_probe_detail = rep.summary
