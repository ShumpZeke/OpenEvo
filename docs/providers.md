# Providers

Last full re-probe: **2026-08-26**, from this repository.

## How to read the claims in this document

Four words, used strictly. They are not synonyms, and the difference between
the first and the last is the difference between knowing and guessing.

| Word | Means |
|---|---|
| **VERIFIED** | We made the call and saw the result, on the date given. |
| **CATALOGUE-VERIFIED** | The id came from the provider's own live listing. We have not called it. |
| **DOCUMENTED** | Official documentation says so. We have not tested it. |
| **UNVERIFIED** | Neither. Recorded because it may be worth checking, not because it is true. |

Nothing in this repository holds a credential for any provider except OpenCode
Zen, which serves some models without one. **Every free-tier claim below is
therefore DOCUMENTED or UNVERIFIED, never VERIFIED**, and no code path may
upgrade one.

---

## Current policy: NVIDIA NIM is the primary provider

Operator decision, 2026-08-27. NIM leads every role chain in both routing
layers — `oe_max/roles.py` for the broker and
`control_plane/providers/profiles.py` for the control plane.

It is also the best-evidenced choice available here: NIM is the only provider in
this repo whose models were probed **individually with a real key**, and four of
the nine configured ids did not survive that probe. Five serve:

| model | measured |
|---|---|
| `nvidia/nemotron-3-super-120b-a12b` | **732 ms** with tools — fastest working route measured on any provider, free or paid |
| `nvidia/nemotron-3-ultra-550b-a55b` | 4.5 s with tools — flagship reasoner, keeps reasoning out of the visible budget |
| `nvidia/nemotron-3-nano-30b-a3b` | serves |
| `moonshotai/kimi-k3` | 11.5 s with tools — the code specialist |
| `deepseek-ai/deepseek-v4-flash-0731` | 51 s — strong, and slow enough to matter |

Four did not, and are disabled rather than deleted so the reason stays readable:
`openai/gpt-oss-120b` hangs (0 bytes after 190 s and again after 230 s),
`nvidia/nemotron-3.5-lightning-30b-a3b` returns 400 "DEGRADED function cannot be
invoked", `mistralai/codestral-22b-instruct-v0.1` returns 404 "Not found for
account" (an entitlement, which a catalogue cannot express), and
`minimaxai/minimax-m3` returns 429 on every attempt including after a 45 s idle
gap — an allowance, not a burst limit.

**Leading with a key-gated provider is safe here** because a route whose
credential is absent is filtered out rather than attempted. With no
`NVIDIA_API_KEY`, every NIM entry drops out of the chain and the keyless
OpenCode Zen routes serve instead. Keep that tail: without it, a checkout with
no credential would have nothing left.

**Ox Alpha is removed from service.** Not disabled — removed. It was withdrawn
by the provider on 2026-08-26 (see below) and taken out of this repo entirely on
2026-08-27. Two routes pointed at it, Zen's `x-preview-f-free` and an alternate
`stealth/ox-alpha` through OpenRouter, and the second outlived the first by a
day because only the first was obvious. `tests/oe_max/test_roles.py` and
`tests/evolution/test_providers.py` fail if either returns.

---

## What changed on 2026-08-26, and why it matters

The configured primary route had stopped existing, and nothing noticed.

**Ox Alpha (`x-preview-f-free`) is withdrawn from OpenCode Zen.** VERIFIED:

- absent from `GET /zen/v1/models` (63 models listed, not among them)
- `POST /chat/completions` returns
  `{"type":"ModelError","message":"Model x-preview-f-free is not supported"}`

That is removal, not gating. A paid Zen model on the same endpoint answers
`{"type":"AuthError","message":"Missing API key."}` instead, so the two are
distinguishable and this one is not a credential problem.

It headed every chain in both routing layers. Every request spent attempts on
a model the provider had stopped acknowledging.

**Two NVIDIA NIM fallbacks were never real.** VERIFIED: neither
`deepseek-ai/deepseek-v4-pro` nor `qwen/qwen2.5-coder-32b-instruct` appears in
NVIDIA's catalogue. NIM hosts **no Qwen model at all**. The catalogue is public
— `GET https://integrate.api.nvidia.com/v1/models` needs no credential — so a
single unauthenticated request would have caught this at any point.

The lesson is in the tooling now, not only in this file: `Registry.reconcile()`
crosses the live listing against what is configured on every discovery and
disables what is no longer listed. Run against the real endpoints it
rediscovered all three of the above on its own.

---

## Verified working today

Measured through `POST https://opencode.ai/zen/v1/chat/completions` with **no
Authorization header**, 2026-08-26. All four also pass a tools probe.

| Model | Latency | Reasoning tokens | Note |
|---|---|---|---|
| `nemotron-3-ultra-free` | 3.3 s | 39 | Strongest keyless route. **Primary.** |
| `hy3-free` | 2.1 s | 43 | |
| `laguna-s-2.1-free` | 1.6 s | **0** | Only free Zen route with no hidden reasoning; reports a prompt-cache hit. |
| `nemotron-3.5-lightning-free` | 7.6 s | 64/64 → truncated | Named "lightning", measured slowest. Needs a large `max_tokens`. |

Those four differ **in kind**, not only in quality. A model that spends its
budget thinking is what you want proposing a mutation and precisely what you do
not want ranking two candidates — there the reasoning buys latency and
truncation risk and nothing else. That is why routing is per role.

Measured end to end through the broker, the gap is wider than the raw probe
suggests: the judge route answered in **859 ms** where the reasoner took
**8,158 ms**.

### Under sustained load

VERIFIED during an 8-iteration evolution run on 2026-08-26: the primary
returned **50% success over 4 requests**, the failures being
`[502] Upstream error from Nvidia: Service temporarily overloaded`, at a 62 s
average. Free routes are flaky under real load; the failover chain is not
decoration.

---

## Role routing

Roles are addressed by broker alias, so the engine picks one by naming a model
and needs no code change.

| Alias | Role | Chain head | Why |
|---|---|---|---|
| `oe-max-primary` | reasoner | `opencode_zen/nemotron_ultra` | What every shipped config names; kept pointing at mutation generation so existing measurements stay comparable. |
| `oe-max-reasoner` | reasoner | `opencode_zen/nemotron_ultra` | Hard reasoning. |
| `oe-max-coder` | coder | `opencode_zen/nemotron_ultra` | Leads keyless; NIM's Kimi K3 and Codestral rank behind it, being key-gated. |
| `oe-max-judge` | judge | `opencode_zen/laguna` | Zero reasoning tokens. Ranking does not need hidden thought. |
| `oe-max-fast` | fast | `opencode_zen/laguna` | Latency-bound work. |

**Preference is an ordering, never a filter.** Each role chain is its
preference followed by every other configured route, so no role can be starved
while usable capacity sits idle. Key-gated routes sit behind keyless ones
everywhere, so the shipped configuration works with no credentials at all and
improves when credentials are added.

---

## Single-model mode

Every role normally has its own chain, and failover keeps a run alive when a
route dies. That is the right default and the wrong shape for three things:

* **Judging a model.** "Is kimi-k3 any good here?" cannot be answered by a run
  where three other models also served requests.
* **Comparing two.** Every arm of an A/B has to be one model, or the comparison
  measures the chain.
* **Wanting a specific model.** Sometimes there is nothing to argue about.

So it is a mode. Off by default; on, every role resolves to the one chosen route.

```bash
curl -s 127.0.0.1:8787/v1/oe-max/single-model            # state + candidates
```

```bash
curl -s -X POST 127.0.0.1:8787/v1/oe-max/single-model \
  -H 'Content-Type: application/json' -d '{"model":"kimi"}'
```

```bash
curl -s -X POST 127.0.0.1:8787/v1/oe-max/single-model \
  -H 'Content-Type: application/json' -d '{"model":null}'   # back to chains
```

`OE_MAX_SINGLE_MODEL` seeds it at startup. The Control Center's **Models** page
has a picker, which is the same thing with fewer quotes.

### It pins, and a pin fails rather than substituting

If the chosen model cannot be served, requests fail with a 503 naming it. They
do **not** quietly fall back to a chain. A run that reports "single model:
kimi-k3" while three others answered is worse than a run that stops, and
preventing that is the whole reason the mode exists.

The status endpoint reports the mode, so a recorded result can be interpreted
later — `single_model.enabled`, the resolved route, and `ok`, which is false
when the mode is on and its selection has become unservable.

### Matching is by substring, and ambiguity is refused

Operators type `kimi`, not `moonshotai/kimi-k3`. A query matching more than one
route is refused with what it matched, rather than guessed at:

```
'nemotron' matches 6 models (nemotron-3-ultra-free, nvidia/nemotron-3-nano-30b-a3b, …);
be more specific
```

An exact `model_id` or `model_key` always wins over a longer substring, so a
fully-typed id is never ambiguous. Refusal happens when you type it, not at the
next request.

## NVIDIA NIM

CATALOGUE-VERIFIED 2026-08-26: 83 models, read from the public listing.
Inference **UNVERIFIED** — no `NVIDIA_API_KEY` exists here, so not one call has
been made. `available` stays `None`, never `True`.

Strongest hosted models relevant to this project:

| Model id | Role |
|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b` | flagship reasoner |
| `openai/gpt-oss-120b` | strong open-weight reasoner |
| `moonshotai/kimi-k3` | agentic / coding |
| `deepseek-ai/deepseek-v4-flash-0731` | strong general |
| `nvidia/nemotron-3-super-120b-a12b` | mid |
| `minimaxai/minimax-m3` | mid |
| `mistralai/codestral-22b-instruct-v0.1` | code-specialised |
| `nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia/nemotron-nano-3-30b-a3b` | fast tier |

Absent, and worth knowing: **no Qwen models**, **no GLM models**. Note the
date-suffix convention — `deepseek-v4-flash-0731`, not `deepseek-v4-flash`.

To use it: set `NVIDIA_API_KEY`, then `POST /v1/oe-max/verify` on the broker.
Discovery reconciles the configured ids against the live catalogue and smoke-
tests what survives. The 48 RPM contract is enforced by the shared limiter at
44/60s; watch the dashboard's rolling-window gauge on the first real run, since
that invariant has 17 property tests and has never met the real endpoint.

---

## Adding a provider

`configs/oe_max/providers.yaml`. Fifteen providers ship, key-gated, so the ones
without credentials are inert and cost nothing.

**The catalogue never names a model.** It names *patterns of interest*, and
concrete ids are materialised from each provider's own live listing at
discovery time. This project has been bitten three times by ids written from
memory, and a pattern that matches nothing yields no routes and no error —
which is correct for an unsatisfied preference. The trade is deliberate: a
mistyped pattern fails silently where a mistyped id would not, but a silent
absence costs one unused provider while a confident wrong id costs every
request routed to it.

Endpoint liveness for all fifteen was VERIFIED on 2026-08-26. Six list their
catalogues without a credential: OpenCode Zen (63), NVIDIA NIM (83),
Hugging Face router (135), ModelScope (47), Chutes (14), SambaNova (7).

| Provider | Env var | Free tier | Confidence |
|---|---|---|---|
| `groq` | `GROQ_API_KEY` | Free plan, no card. Per-model, small: gpt-oss-120b at 30 RPM / 1K RPD / 8K TPM / 200K TPD. Org-wide, so extra keys do not help. | DOCUMENTED |
| `cerebras` | `CEREBRAS_API_KEY` | Reported ~1M tokens/day, ~30 RPM, resets 00:00 UTC, **~8K context cap**. | UNVERIFIED |
| `gemini` | `GEMINI_API_KEY` | Free tier with per-model limits. **Free-tier prompts may be used to improve Google's products** — material for private code. | DOCUMENTED |
| `mistral` | `MISTRAL_API_KEY` | "Experiment" tier, phone verification. Devstral/Codestral relevant. | DOCUMENTED |
| `huggingface` | `HF_TOKEN` | Small monthly credit; routes to third parties, so terms depend on who serves. | DOCUMENTED |
| `sambanova` | `SAMBANOVA_API_KEY` | Credits, not a recurring allowance. | UNVERIFIED |
| `modelscope` | `MODELSCOPE_API_KEY` | Reported daily free calls. CN region — 1.8 s just to list from here. | UNVERIFIED |
| `chutes` | `CHUTES_API_KEY` | Free access reported, and reported withdrawn. | UNVERIFIED |
| `zai_glm` | `ZAI_API_KEY` | Has previously offered a free GLM flash model. | UNVERIFIED |
| `dashscope_qwen` | `DASHSCOPE_API_KEY` | Time-limited allowance per model on activation. | UNVERIFIED |
| `siliconflow` | `SILICONFLOW_API_KEY` | Has previously carried free-tagged models. | UNVERIFIED |
| `nebius` | `NEBIUS_API_KEY` | Signup credits. | UNVERIFIED |
| `deepseek` | `DEEPSEEK_API_KEY` | **None.** Listed for being cheap and strong. | DOCUMENTED |
| `moonshot` | `MOONSHOT_API_KEY` | **None.** Kimi is strong at agentic coding. | DOCUMENTED |
| `minimax` | `MINIMAX_API_KEY` | **None.** | DOCUMENTED |

---

## Rejected / dead / misleading

Recorded so nobody spends another afternoon rediscovering them. Remove an entry
only with evidence it works again. The machine-readable copy is the `retired:`
block of `configs/oe_max/providers.yaml`.

| Endpoint | Finding | Verified |
|---|---|---|
| **GitHub Models** | HTTP **410**, `github_models_retirement_brownout` — "temporarily unavailable as part of a scheduled retirement brownout". A brownout is the rehearsal for a shutdown, not an outage to wait out. Widely recommended as a free endpoint; it is not one. | 2026-08-26 |
| **Targon** | HTTP **410 Gone**, empty body. | 2026-08-26 |
| **Zen `x-preview-f-free`** (Ox Alpha) | Withdrawn. `ModelError: Model ... is not supported`. Was the configured primary. | 2026-08-26 |
| **Zen `deepseek-v4-flash-free`** | Listed but unserveable: HTTP 400 "Model is unavailable", unchanged since 2026-08-25. The original evidence that a listing is not a working model. | 2026-08-26 |
| **Zen `mimo-v2.5-free`** | HTTP 429 `FreeUsageLimitError` on every attempt. A shared free pool that is empty, not a per-account rate limit. | 2026-08-26 |
| **Zen `muse-spark-1.2-contributor-free`** | Newly listed, returns HTTP 500. The name suggests contributor-gating. | 2026-08-26 |
| **NIM `deepseek-ai/deepseek-v4-pro`** (bare id) | Not in NVIDIA's catalogue. Was configured as the strong fallback. **The date-suffixed form does exist** — see the row below; the original note said no v4-pro existed at all, which is no longer true. | 2026-08-26, corrected 2026-08-29 |
| **NIM `deepseek-ai/deepseek-v4-pro-0813`** | Listed, serves, and excluded **for being slow**: HTTP 200 with a real answer, median **183.5 s** on a one-word prompt — 115× kimi-k3 and 274× the 120B on the same prompt in the same minute. Kept configured with `available=False` rather than deleted, so nobody adds it back wondering why it was left out. | 2026-08-29 |
| **NIM `moonshotai/kimi-k2.6`** | In the listing, HTTP **404** on use. The third model to prove a listing is not a promise. | 2026-08-29 |
| **NIM `qwen/qwen2.5-coder-32b-instruct`** | Not in NVIDIA's catalogue. NIM hosts no Qwen model. | 2026-08-26 |

### A per-model allowance is a third thing again

`moonshotai/kimi-k3` is not in the table above because it works: HTTP 200,
median **1.59 s**, and it keeps its reasoning in a separate channel. It is worth
knowing about anyway.

Roughly **ten calls over twenty minutes** exhausted its allowance on this
account, after which it refused with HTTP 429 in **0.2 s** — instant, not
queued — for at least five minutes. Three nemotron routes returned 200
throughout, which is what identifies it: not the account, not our limiter, and
not something retrying will fix.

It therefore sits *second* on the reasoner and coder chains rather than leading
them. Leading with it worked — failover served on attempt 2 — but spent one of
four attempts on a refusal for every request. Second, it costs nothing while
exhausted and is used the moment the allowance returns.

Measure the allowance, not just the latency. A latency table alone would tell
you to promote exactly the model you should not.

### An exhausted allowance is not a rate limit

Both arrive as HTTP 429, and Zen's free-limit message even reads *"Rate limit
exceeded. Please try again later."* — advice that is wrong in this case. Only
the error **type** distinguishes them.

Retrying cannot refill a pool, so the old behaviour spent the whole retry
budget collecting the identical error four times before failing over. A 429
whose body names an exhausted allowance is now `Outcome.FREE_LIMIT_EXHAUSTED`:
not retryable, fails over immediately, and parks the route for 15 minutes.

VERIFIED through the broker: a pinned request to `mimo-v2.5-free` returns
`free_limit_exhausted` at **attempt 1 of a possible 4**, parks the route for
900 s, and leaves the circuit **closed** — the provider is not blamed for our
own spent allowance.

---

## Caveats worth keeping

- **Zen serves some models with no credential.** Requiring a key would disable
  the working primary. `requires_key=False` is deliberate on that provider —
  but it is per-provider, not per-model: Zen's `deepseek-v4-flash` is paid and
  returns 401 without a key, and was mistakenly configured keyless.
- **A listed model is not a working model.** Two-stage discovery exists for
  this. Do not simplify it back to one stage.
- **An unlisted model is not a model.** That is the converse, and it is what
  `reconcile()` adds. Ox Alpha's withdrawal was visible in a listing for free.
- **Free status is three-valued and the third value is UNKNOWN.** The UI must
  not render an unprobed route as free.
- **`urllib` is Cloudflare-blocked by Zen** (`403 error code: 1010`); `httpx`
  and `curl` are not. Do not reach for `urllib` in new probing code.
