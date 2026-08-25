# Providers

## Policy

Default preference, per SOURCE_OF_TRUTH section 16.1:

```
1. OpenCode Zen / Ox Alpha Free    ← PRIMARY / PREFERRED
2. NVIDIA NIM strong model         ← PRIMARY FALLBACK
3. other free/cheap Zen models
4. other OpenAI-compatible endpoints
5. local compatible endpoint
```

Routing is **capability-aware as well as health-aware**. That distinction is
load-bearing here, not decoration — see below.

## Verified facts (2026-08-25)

Both checked against current official sources rather than assumed.

**Ox Alpha Free**
- model id `x-preview-f-free` (alias `ox-alpha-free` on the Go route)
- endpoint `https://opencode.ai/zen/v1`, OpenAI-compatible
- 1,048,576-token context, up to 131,072 completion tokens
- listed at $0 input/output/cached
- documented as *"free on OpenCode for a limited time"* — **not permanent**

**Tool calling is currently broken on it.**
[anomalyco/opencode#44300](https://github.com/anomalyco/opencode/issues/44300),
open: any request containing a `tools` array returns *"Upstream request failed:
Endpoint is unavailable."* Plain chat completions succeed in about 3 seconds.
`nemotron-3-ultra-free` and `deepseek-v4-flash` handle tools correctly on the
same route, so this is model-specific, not infrastructure.

## What that implies for routing

OpenEvolve's mutation and reasoning calls are plain completions, so Ox Alpha is
used there — which is the majority of model traffic in a run. Agent roles
(orchestrator, deep coding, planning, review, architecture, explore) require
function calling, so they route to a verified tools-capable model instead.

Encoding "Ox Alpha for everything" would honour the stated preference and then
fail every agent run.

| Role | Requires | Default route |
|---|---|---|
| mutation | chat | `zen-ox-alpha-free` |
| evaluator | chat | `zen-ox-alpha-free` |
| research | chat | `zen-deepseek-v4-flash` |
| parallel_worker | chat | `zen-deepseek-v4-flash` |
| orchestrator | chat + **tools** | `zen-nemotron-3-ultra-free` |
| deep_coding | chat + **tools** | `zen-nemotron-3-ultra-free` |
| planning | chat + **tools** | `zen-nemotron-3-ultra-free` |
| review | chat + **tools** | `zen-nemotron-3-ultra-free` |
| architecture | chat + **tools** | `zen-nemotron-3-ultra-free` |
| explore | chat + **tools** | `zen-deepseek-v4-flash` |
| emergency | chat | `nim-deepseek-v4-pro` |

The Models page shows the exclusion reason on every role, including the issue
reference, so the operator is never left wondering why their preferred model is
not serving a role.

## Self-correcting

The provider doctor (`providers/doctor.py`) probes each enabled profile live:
credential presence, a real chat completion, a real tools request, latency and
HTTP status. Results are written into `verified_capabilities`.

The moment Ox Alpha's tool support is fixed upstream, the next doctor run
records `TOOLS` as verified and the router can promote it into agent roles with
**no code change**. Nothing about the issue is hardcoded as permanent.

A probe that cannot run — no credential, no network — is reported `SKIPPED` with
the reason. It is never reported as a pass, and never as a failure of the model.

## Free status

Three-valued and defaulting to `UNKNOWN`:

| Value | Meaning | UI |
|---|---|---|
| `FREE_LIMITED_TIME` | provider documents free-for-now | amber "free (limited time)" with caveat |
| `FREE` | genuinely free (e.g. local hardware) | green "free" |
| `PAID` | billed | "paid" |
| `UNKNOWN` | not probed | grey "unknown" |

A two-valued flag would make an unprobed model read as free. Acceptance
criterion 27 forbids presenting Ox Alpha as permanently or unlimitedly free, and
`test_free_status_is_never_claimed_permanent` checks the shipped text for
claims of unlimited access.

## Health and failover

Per model, over a rolling window: success rate, p50 latency, 429 pressure,
consecutive failures, in-flight count.

Selection filters, then sorts:

- **Filter** — enabled, credential present, required capabilities satisfied,
  circuit closed, under its concurrency limit.
- **Sort** — position in the role's chain, then live health, then priority.

429s are weighted more heavily than plain failures: a rate limit says the route
will keep refusing, not that one call was unlucky. After N consecutive failures
the circuit opens with exponential backoff (capped), traffic sheds to the next
route in the chain, and the model is retried after cooldown. Circuits can be
reset from the Models page.

An unused model scores optimistically, so a fresh fallback can win a route and
prove itself rather than being locked out by having no history.

When nothing can serve a role, `NoRouteAvailable` carries the reason for each
excluded model — `missing credential NVIDIA_API_KEY`, `lacks capability: tools`,
`circuit open for 47s` — rather than a bare failure.

## Cost

`input_cost_per_mtok` / `output_cost_per_mtok` are `None` when unknown, and the
UI renders unknown. A fabricated `0.0` would understate real spend, which is
worse than admitting the number is not known.
