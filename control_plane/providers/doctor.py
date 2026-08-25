"""
Provider doctor.

Replaces assumptions with measurements. For each enabled profile it probes what
actually happens right now — reachability, auth, latency, whether the model
answers a trivial completion, whether it accepts a `tools` array, and how it
behaves when rate limited.

This exists because the two things the routing policy depends on are both
documented as changeable: Ox Alpha's free status is time-limited, and its tool
support is currently broken. A router built on the docs alone would be wrong
within a week. A router built on probes self-corrects.

Nothing here fabricates a result. A probe that cannot run (no credential, no
network) is reported as SKIPPED with the reason, never as a pass.
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
            "free_status": self.free_status.value,
            "free_note": self.free_note,
            "latency_ms": self.latency_ms,
            "preferred": self.preferred,
            "summary": self.summary,
        }


class ProviderDoctor:
    def __init__(self, timeout_s: float = 25.0) -> None:
        self.timeout_s = timeout_s

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

        # 1. Credential presence — a local endpoint legitimately needs none.
        if profile.secret_ref:
            if profile.has_secret():
                rep.probes.append(Probe("credential", ProbeResult.PASS,
                                        f"{profile.secret_ref} present"))
            else:
                rep.probes.append(Probe(
                    "credential", ProbeResult.SKIPPED,
                    f"{profile.secret_ref} not set — live probes cannot run. "
                    f"This is reported as unverified, not as failure.",
                ))
                rep.summary = "credential missing; not verified"
                rep.available = False
                self._emit(rep)
                return rep
        else:
            rep.probes.append(Probe("credential", ProbeResult.SKIPPED, "no credential required"))

        # 2. Plain chat completion.
        chat = await self._probe_chat(profile, with_tools=False)
        rep.probes.append(chat)
        rep.latency_ms = chat.latency_ms
        if chat.result is ProbeResult.PASS:
            rep.available = True
            rep.verified_capabilities.append(Capability.CHAT)

        # 3. Tool calling — the probe that matters most for agent roles.
        if probe_tools and rep.available:
            tools = await self._probe_chat(profile, with_tools=True)
            tools.name = "tools"
            rep.probes.append(tools)
            if tools.result is ProbeResult.PASS:
                rep.verified_capabilities.append(Capability.TOOLS)

        # 4. Free status. We can confirm "not free" from a billing signal, but
        #    we never upgrade to "free forever" — the docs say limited time.
        if rep.available and profile.free_status is FreeStatus.FREE_LIMITED_TIME:
            rep.free_note = (
                profile.free_note
                or "Free for a limited time per provider docs; verify before relying on it."
            )

        rep.summary = self._summarize(rep)
        self._emit(rep)
        return rep

    async def _probe_chat(self, profile: ModelProfile, with_tools: bool) -> Probe:
        """Issue one real minimal request against the endpoint."""
        name = "chat_with_tools" if with_tools else "chat"
        key = os.environ.get(profile.secret_ref) if profile.secret_ref else None
        url = profile.api_base.rstrip("/") + "/chat/completions"
        body: Dict[str, Any] = {
            "model": profile.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 16,
            "temperature": 0,
        }
        if with_tools:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": "noop",
                    "description": "A no-op probe function.",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": [],
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
                ok = bool(data.get("choices"))
            except json.JSONDecodeError:
                return Probe(name, ProbeResult.FAIL, "200 but body was not JSON",
                             latency, status)
            return Probe(
                name, ProbeResult.PASS if ok else ProbeResult.FAIL,
                "completion returned" if ok else "200 but no choices",
                latency, status,
            )
        if status == 429:
            return Probe(name, ProbeResult.FAIL, "rate limited (429)", latency, status)
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
        POST without adding a hard dependency on an async HTTP client.

        urllib in a thread keeps the doctor usable in a bare environment; the
        engine's own OpenAI client is unaffected by this choice.
        """
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
            failed = [p for p in rep.probes if p.result is ProbeResult.FAIL]
            return failed[0].detail if failed else "unavailable"
        caps = ", ".join(c.value for c in rep.verified_capabilities) or "none"
        return f"available ({caps})" + (f", {rep.latency_ms:.0f}ms" if rep.latency_ms else "")

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
        # Only record verified capabilities when the probe actually ran; a
        # skipped probe must not erase declared capabilities.
        if rep.available:
            p.verified_capabilities = list(rep.verified_capabilities)
        if rep.free_note:
            p.free_note = rep.free_note
