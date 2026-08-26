# Providers

## Status, 2026-08-26

**Four of the five configured remote routes were dead at the same time.** This
document was rewritten from live probes on that date, not from the previous
version of itself.

| configured route | model id | what it did |
|---|---|---|
| `zen-ox-alpha-free` | `x-preview-f-free` | HTTP 401 *"Model x-preview-f-free is not supported"*, **and absent from Zen's catalogue** — withdrawn |
| `zen-deepseek-v4-flash` | `deepseek-v4-flash` | HTTP 401 *"Missing API key."* — configured keyless; it is Zen's paid tier |
| `nim-deepseek-v4-pro` | `deepseek-ai/deepseek-v4-pro` | **absent from NIM's catalogue** |
| `nim-qwen25-coder-32b` | `qwen/qwen2.5-coder-32b-instruct` | **absent from NIM's catalogue** — NIM lists no qwen model at all now |
| `zen-nemotron-3-ultra-free` | `nemotron-3-ultra-free` | healthy — 3/3 chat, 3/3 tools |

Every role chain led with Ox Alpha, so the shipped default configuration routed
every role to a model that no longer existed. That is the failure this page and
`control_plane/providers/catalog.py` exist to stop recurring.

## Policy

Per SOURCE_OF_TRUTH section 16.1, in terms of properties rather than names —
which is the lesson of the table above:

```
1. strongest currently-free OpenCode Zen route   ← PRIMARY / PREFERRED
2. NVIDIA NIM strong model                       ← PRIMARY FALLBACK
3. other free/cheap Zen models
4. other OpenAI-compatible endpoints
5. local compatible endpoint
```

Routing is **capability-aware as well as health-aware**, which remains
load-bearing: a route can be healthy, preferred, and unable to serve a tools
request. Ox Alpha was exactly that under anomalyco/opencode#44300; Laguna is
that today.

## Measured routes

All probed against `https://opencode.ai/zen/v1` on 2026-08-26 **with no
Authorization header**, three repeats of each of chat and tools. "tool calls"
counts replies that actually contained a `tool_calls` entry — not replies that
merely returned HTTP 200 to a request carrying a `tools` array.

| model | chat | chat p50 | tool calls | tools p50 | reasoning tokens | `cost` |
|---|---|---|---|---|---|---|
| `nemotron-3-ultra-free` | 3/3 | 4.04 s | 3/3 | 7.13 s | 23–34 | `"0"` |
| `hy3-free` | 3/3 | 2.34 s | 3/3 | 2.86 s | 13–60 | `"0"` |
| `laguna-s-2.1-free` | 8/10 | 1.74 s | **1/3** | 2.27 s | 0 | `"0"` |
| `nemotron-3.5-lightning-free` | 3/3 | 2.82 s | 3/3 | 10.41 s | 111–125 | `"0"` |
| `mimo-v2.5-free` | 0/3 | — | — | — | — | — |
| `big-pickle` | 0/3 | — | — | — | — | — |
| `muse-spark-1.2-contributor-free` | 0/1 | — | — | — | — | — |
| `deepseek-v4-flash-free` | 0/1 | — | — | — | — | — |

- `mimo-v2.5-free` and `big-pickle` returned HTTP 429 `FreeUsageLimitError` on
  every anonymous attempt. Both are in the catalogue, so the models exist; the
  shared free pool would not serve us. Configured as key-requiring on the
  evidence that keyless access does not work — **not** on evidence that a key
  helps, which is unverified.
- `muse-spark-1.2-contributor-free` returned HTTP 500. `deepseek-v4-flash-free`
  returned HTTP 400 *"Model is unavailable"* despite being listed — the standing
  example of why being listed is not being served.
- `laguna-s-2.1-free` is the fastest route measured and the least reliable: two
  of ten chat attempts and two of three tools attempts returned HTTP 503
  *"Upstream request failed: Endpoint is unavailable"* — the same error Ox
  Alpha's tool bug produced. It is used for chat and **does not declare tools**.

### Role assignments

| Role | Requires | Default route |
|---|---|---|
| mutation | chat | `zen-nemotron-3-ultra-free` |
| evaluator | chat | `zen-nemotron-3-ultra-free` |
| research | chat | `zen-hy3-free` |
| parallel_worker | chat | `zen-hy3-free` |
| orchestrator | chat + **tools** | `zen-nemotron-3-ultra-free` |
| deep_coding | chat + **tools** | `zen-nemotron-3-ultra-free` |
| planning | chat + **tools** | `zen-nemotron-3-ultra-free` |
| review | chat + **tools** | `zen-nemotron-3-ultra-free` |
| architecture | chat + **tools** | `zen-nemotron-3-ultra-free` |
| explore | chat + **tools** | `zen-hy3-free` |
| emergency | chat | `nim-nemotron-3-ultra-550b` |

Completion roles lead with the largest model rather than the fastest. That is
the operator's standing preference — strongest free route first — and latency
alone is not grounds to change it. Whether Laguna's 2.3x speed advantage buys
more improvement per second than Ultra's extra capacity is exactly what
NEXT_TASKS T1 exists to answer, now against two routes that both work.

The latency-sensitive chains lead with `hy3-free` rather than the quicker
Laguna: one failure in five costs a retry, which is worth more than 0.6 s.

## Catalogue reconciliation

`control_plane/providers/catalog.py`, run by the doctor on every pass. It fetches
`GET {api_base}/models` — Zen, NIM and OpenRouter all serve that listing without
a credential — and says where each configured id stands.

It runs **before** the credential check, deliberately. An uncredentialled NIM
route used to report "NVIDIA_API_KEY not set" and stop, which was true and
useless: both NIM ids were absent from the catalogue and no key would have
helped. A missing id also produces near-miss suggestions, so an operator asking
for `nemotron-3-ultra-free` on NIM is handed `nvidia/nemotron-3-ultra-550b-a55b`.

Two asymmetries keep it evidence rather than a gate, and both were observed here:

- **Listed does not imply served.** Zen lists `deepseek-v4-flash-free` and
  answers *"Model is unavailable"* for it.
- **Absent does not imply unserved.** Ox Alpha was a stealth preview: served for
  weeks while never appearing in the listing.

So the live request stays the authority and `ABSENT` never disables anything. A
catalogue that could not be fetched is `UNKNOWN`, never `ABSENT` — reporting a
network failure as "the model is gone" would retire healthy routes.

## The doctor

`control_plane/providers/doctor.py` probes each enabled profile live: catalogue
standing, credential presence, a real chat completion, a real tools request,
latency and HTTP status. Results are written into `verified_capabilities`.

Three rules it did not have before 2026-08-26:

1. **A tools probe must observe a `tool_calls` entry.** Any 200 carrying
   `choices` used to count. `nemotron-3-ultra-free` answers a too-small tools
   request with 200, a null `finish_reason`, an empty message and no tool call,
   and that was recorded as tool support verified.
2. **Every attempt must pass.** Tools are probed twice and both must emit a
   call. Laguna emits one on roughly one attempt in three; probed once, it
   promoted itself into every agent role a third of the time.
3. **The probe gets room to answer.** The budget was 16 tokens, set when these
   routes were plain completion models. On a reasoning model the whole budget
   goes to hidden reasoning and the reply is empty. It is now 512 for chat and
   1024 for tools, and an empty 200 is diagnosed as truncation rather than
   passed as healthy.

A probe that cannot run is `SKIPPED` with the reason — no credential, a
Cloudflare 1010 fingerprint block, or a 429 from a saturated free pool. It is
never reported as a pass, and never as a failure of the model.

**A failed probe now takes the route out of selection**, for `probe_ttl_s`
(default 600 s). Until this existed the doctor could establish that a route was
returning 503 and change nothing — the profile kept its declared capabilities
and kept leading its chain, and the circuit breaker had to rediscover the outage
with real requests. The suppression expires on purpose: a provider blip at 09:00
must not keep a healthy route off the table all day.

The moment a capability is fixed upstream, the next doctor run records it and
the router promotes the model with **no code change**. The *chain order* is a
stated preference and does not self-correct; changing it stays a deliberate edit.

## Free status

Three-valued and defaulting to `UNKNOWN`:

| Value | Meaning | UI |
|---|---|---|
| `FREE_LIMITED_TIME` | provider serves it at no charge now, with no commitment | amber "free (limited time)" with caveat |
| `FREE` | genuinely free (e.g. local hardware) | green "free" |
| `PAID` | billed | "paid" |
| `UNKNOWN` | not probed | grey "unknown" |

Zen's free replies carry `cost: "0"` in the response body, which is good
evidence they are free *now* and none at all that they will stay free. Every Zen
free route is therefore `FREE_LIMITED_TIME`, never `FREE`. Ox Alpha is the
standing counter-example — documented free, then withdrawn — and
`test_no_profile_claims_permanent_or_unlimited_free_access` checks the shipped
text of every profile, not just one, for claims of unlimited access.

## Other free endpoints

Checked on 2026-08-26. "Catalogue" is whether `GET /models` answers without a
credential — which is what makes a provider safe to add to the routing table,
because its model ids can be verified rather than remembered.

| Provider | OpenAI-compatible base | Catalogue keyless? | In the table? |
|---|---|---|---|
| OpenCode Zen | `https://opencode.ai/zen/v1` | yes, 63 models | yes — and it serves keyless too |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | yes, 83 models | yes, ids catalogue-checked, serving unverified |
| OpenRouter | `https://openrouter.ai/api/v1` | yes, 416 models, 25 priced at $0 | yes, one `:free` route, serving unverified |
| DeepInfra | `https://api.deepinfra.com/v1/openai` | yes, 189 models | no — no free tier established |
| SambaNova | `https://api.sambanova.ai/v1` | yes, 7 models | no — free tier not verified |
| Chutes | `https://llm.chutes.ai/v1` | yes, 14 models | no — priced per model |
| Groq | `https://api.groq.com/openai/v1` | no (401) | no — see below |
| Cerebras | `https://api.cerebras.ai/v1` | no (403) | no — see below |
| Google AI Studio | `https://generativelanguage.googleapis.com/v1beta/openai` | no (404 unauthenticated) | no — see below |
| Mistral | `https://api.mistral.ai/v1` | no (401) | no |
| Together | `https://api.together.xyz/v1` | no (401) | no |
| GitHub Models | `https://models.github.ai` | **HTTP 410, retirement brownout** | no |
| Nebius / Scaleway / SambaNova / ArliAI / Cohere | various | no (401/403) | no |

Groq, Cerebras and Google AI Studio all document permanent free tiers with no
credit card, and all three are worth keying. They are **deliberately not shipped
as profiles**: their catalogues cannot be read without a key, so their model ids
would have to be written from memory — which is exactly how four dead routes got
into the table in the first place. Add a profile once you hold a key and can read
`/models`; the doctor reconciles it from then on.

Their base URLs and env var names are in `.env.example`.

## Health and failover

Per model, over a rolling window: success rate, p50 latency, 429 pressure,
consecutive failures, in-flight count.

Selection filters, then sorts:

- **Filter** — enabled, credential present, no fresh failing doctor verdict,
  required capabilities satisfied, circuit closed, under its concurrency limit.
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
`circuit open for 47s`, `provider doctor found this route failing 12s ago` —
rather than a bare failure.

## Cost

`input_cost_per_mtok` / `output_cost_per_mtok` are `None` when unknown, and the
UI renders unknown. A fabricated `0.0` would understate real spend, which is
worse than admitting the number is not known. Where `0.0` is recorded,
`cost_basis` states what it is based on — for the Zen free routes, the `cost: "0"`
the provider returned in the response body.
