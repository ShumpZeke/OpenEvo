"""
StdioBrainPort — the OpenCode-native BrainPort.

Used when the Python worker is spawned by the OpenCode TypeScript plugin.

Protocol (JSONL over stdio):
  Worker -> Plugin: {"type":"brain_request","id":"...","request":{BrainRequest dict}}
  Plugin -> Worker: {"type":"brain_response","id":"...","response":{BrainResponse dict}}

The worker's stdout is multiplexed:
  * brain_request  — LLM generation request (worker waits for brain_response)
  * event          — evolution progress events (fire-and-forget)
  * rpc_response   — direct response to plugin's evolve/* commands

The worker's stdin is multiplexed:
  * rpc_request    — plugin's evolve/start, evolve/status, etc.
  * brain_response — plugin's answer to a prior brain_request

This keeps the transport at stdio JSONL, no HTTP microservice.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import AsyncIterator, Dict, Optional

from .capabilities import BrainCapabilities
from .port import BrainPort, BrainPortError
from .types import BrainRequest, BrainResponse


class StdioBrainPort(BrainPort):
    """
    BrainPort that delegates to the host (OpenCode plugin) over stdio.

    The host must run an event loop that reads brain_request lines from the
    worker's stdout and writes brain_response lines to the worker's stdin.

    This port is designed to be used *inside* the worker process only.
    """

    def __init__(
        self,
        *,
        writer: Optional[asyncio.StreamWriter] = None,
        reader: Optional[asyncio.StreamReader] = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.timeout_s = timeout_s
        # When running under the worker's JSONL loop, these are injected.
        # When used standalone, we fall back to sys.stdin/stdout.
        self._writer = writer
        self._reader = reader
        self._caps: Optional[BrainCapabilities] = None
        # Pending brain requests waiting for a response
        self._pending: Dict[str, asyncio.Future] = {}
        self._loop_task: Optional[asyncio.Task] = None

    # -- called by the worker's main JSONL loop ------------------------

    def attach_streams(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    def handle_brain_response(self, msg: Dict) -> None:
        """Called by the worker's stdin dispatcher when a brain_response arrives."""
        rid = msg.get("id")
        fut = self._pending.get(rid) if rid else None
        if fut and not fut.done():
            try:
                resp = BrainResponse.from_dict(msg.get("response") or {})
                fut.set_result(resp)
            except Exception as exc:
                fut.set_exception(exc)

    # -- BrainPort interface -------------------------------------------

    async def generate(self, request: BrainRequest) -> BrainResponse:
        if self._writer is None:
            raise BrainPortError("stdio brain not attached — no writer", retryable=False)

        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut

        payload = {
            "type": "brain_request",
            "id": rid,
            "request": request.to_dict(),
        }
        try:
            line = json.dumps(payload, ensure_ascii=False)
            self._writer.write((line + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as exc:
            self._pending.pop(rid, None)
            raise BrainPortError(f"failed to send brain_request: {exc}", retryable=True) from exc

        try:
            resp: BrainResponse = await asyncio.wait_for(fut, timeout=self.timeout_s)
            return resp
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise BrainPortError(f"brain_request timed out after {self.timeout_s}s", retryable=True) from exc
        finally:
            self._pending.pop(rid, None)

    async def stream(self, request: BrainRequest) -> AsyncIterator[str]:
        # Host may support streaming as multiple brain_response chunks.
        # Fallback to buffered generate() if not.
        resp = await self.generate(request)
        if resp.ok:
            yield resp.content
        else:
            raise BrainPortError(resp.error or "stream failed", retryable=False)

    async def capabilities(self) -> BrainCapabilities:
        if self._caps is not None:
            return self._caps
        # Ask host for capabilities via a synthetic request
        # If host doesn't support it, return minimal
        # The host plugin should inject capabilities on startup; we cache them here
        return BrainCapabilities.minimal()

    def set_capabilities(self, caps: BrainCapabilities) -> None:
        self._caps = caps

    async def close(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
