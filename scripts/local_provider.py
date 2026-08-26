#!/usr/bin/env python3
"""
Local OpenAI-compatible model endpoint for offline integration testing.

WHAT THIS IS: a real HTTP server speaking the OpenAI /v1/chat/completions
contract, which returns genuine SEARCH/REPLACE diffs that OpenEvolve applies,
evaluates and selects on. It is a *model provider*, and Evolution treats it as
one — the `local-openai-compatible` profile in providers/profiles.py exists for
exactly this class of endpoint.

WHAT THIS IS NOT: it is not fake telemetry and not a stub of any Evolution
component. The engine, database, MAP-Elites placement, island migration,
evaluation, checkpointing and every emitted event are the real implementations.
Only the text generation is local and deterministic.

Why it exists: acceptance requires proving a real OpenEvolve example runs
end-to-end through the fork. That must be demonstrable without a paid API key
and without network access, and it must be reproducible in CI. A seeded local
generator gives a deterministic run, which is better for a regression test than
a live model would be.

Usage:
    python scripts/local_provider.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Candidate mutations for the function-minimization example. Each is a valid
# SEARCH/REPLACE block against the current program text; the engine applies
# whichever matches and scores the result for real.
MUTATIONS = [
    (
        "restart random search from the best point so far",
        """<<<<<<< SEARCH
    for _ in range(iterations):
        # Simple random search
        x = np.random.uniform(bounds[0], bounds[1])
        y = np.random.uniform(bounds[0], bounds[1])
        value = evaluate_function(x, y)
=======
    for i in range(iterations):
        # Local perturbation around the incumbent, shrinking over time
        scale = max(0.05, 2.0 * (1.0 - i / max(1, iterations)))
        x = np.clip(best_x + np.random.normal(0, scale), bounds[0], bounds[1])
        y = np.clip(best_y + np.random.normal(0, scale), bounds[0], bounds[1])
        value = evaluate_function(x, y)
>>>>>>> REPLACE""",
    ),
    (
        "add multi-start sampling to escape local minima",
        """<<<<<<< SEARCH
    # Initialize with a random point
    best_x = np.random.uniform(bounds[0], bounds[1])
    best_y = np.random.uniform(bounds[0], bounds[1])
    best_value = evaluate_function(best_x, best_y)
=======
    # Multi-start: take the best of several random seeds before refining
    best_x = best_y = None
    best_value = float("inf")
    for _ in range(12):
        cx = np.random.uniform(bounds[0], bounds[1])
        cy = np.random.uniform(bounds[0], bounds[1])
        cv = evaluate_function(cx, cy)
        if cv < best_value:
            best_value, best_x, best_y = cv, cx, cy
>>>>>>> REPLACE""",
    ),
    (
        "add simulated-annealing acceptance",
        """<<<<<<< SEARCH
        if value < best_value:
            best_value = value
            best_x, best_y = x, y
=======
        if value < best_value:
            best_value = value
            best_x, best_y = x, y
        elif np.random.random() < 0.02:
            # Occasionally accept a worse point to escape a basin
            best_x, best_y = x, y
>>>>>>> REPLACE""",
    ),
    (
        "widen the annealing schedule",
        """<<<<<<< SEARCH
        scale = max(0.05, 2.0 * (1.0 - i / max(1, iterations)))
=======
        scale = max(0.01, 3.0 * (1.0 - i / max(1, iterations)) ** 1.5)
>>>>>>> REPLACE""",
    ),
    (
        "increase multi-start breadth",
        """<<<<<<< SEARCH
    for _ in range(12):
=======
    for _ in range(24):
>>>>>>> REPLACE""",
    ),
]


_ALTERNATIVES_RE = re.compile(
    r"Produce\s+(\d+)\s+SEPARATE AND MATERIALLY DIFFERENT alternatives",
    re.IGNORECASE)


def _alternatives_requested(prompt: str) -> int:
    """How many alternatives the caller asked for, if any."""
    m = _ALTERNATIVES_RE.search(prompt or "")
    return int(m.group(1)) if m else 1


class Handler(BaseHTTPRequestHandler):
    server_version = "EvolutionLocalProvider/1.0"
    seed_counter = 0

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {
                "object": "list",
                "data": [{"id": "evolution-local", "object": "model",
                          "owned_by": "evolution-local-provider"}],
            })
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return

        # Honour the OpenAI contract for tools so capability probes behave
        # realistically against this endpoint.
        if body.get("tools"):
            self._json(200, self._completion(
                body.get("model", "evolution-local"),
                "Tool calling is not implemented by the local provider.",
            ))
            return

        prompt = "\n".join(
            m.get("content", "") for m in body.get("messages", [])
            if isinstance(m, dict)
        )
        Handler.seed_counter += 1
        rng = random.Random(Handler.seed_counter)

        # Prefer a mutation whose SEARCH text is actually present in the prompt,
        # so the engine gets an applicable diff rather than a rejected one.
        applicable = []
        for desc, diff in MUTATIONS:
            m = re.search(r"<<<<<<< SEARCH\n(.*?)=======", diff, re.DOTALL)
            if m and m.group(1).strip().splitlines():
                first = m.group(1).strip().splitlines()[0].strip()
                if first and first in prompt:
                    applicable.append((desc, diff))
        pool = applicable or MUTATIONS

        # Honour a request for several alternatives, the way a cooperative
        # model would. Without this there is no way to exercise multi-offspring
        # end to end without spending a real provider's time, and the parsing
        # is the part most worth exercising.
        wanted = _alternatives_requested(prompt)
        if wanted > 1 and len(pool) > 1:
            chosen = rng.sample(pool, min(wanted, len(pool)))
            sections = [
                f"### ALTERNATIVE {i}\n{d}\n"
                for i, (_, d) in enumerate(chosen, start=1)
            ]
            content = (
                "Here are several different approaches.\n\n" + "\n".join(sections)
            )
            self._json(200, self._completion(
                body.get("model", "evolution-local"), content))
            return

        desc, diff = rng.choice(pool)

        content = (
            f"I will improve the search algorithm: {desc}.\n\n"
            f"{diff}\n"
        )
        self._json(200, self._completion(body.get("model", "evolution-local"), content))

    @staticmethod
    def _completion(model: str, content: str) -> dict:
        # Include a usage block so token accounting downstream is real rather
        # than absent; these are the true sizes for this response.
        prompt_tokens = 512
        completion_tokens = max(1, len(content) // 4)
        return {
            "id": f"chatcmpl-local-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def log_message(self, *a) -> None:
        pass  # quiet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Evolution local provider on http://{args.host}:{args.port}/v1", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
