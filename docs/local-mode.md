# Fully local mode

Run evolution with the model on your own machine and nothing leaving it.

```bash
OE_MAX_LOCAL_ONLY=1 ./scripts/start-broker.sh          # terminal 1
./scripts/run-evolution.sh --config configs/oe_max/local.yaml
```

Windows: `$env:OE_MAX_LOCAL_ONLY = "1"` then `.\scripts\start-broker.ps1`.

No API key. No account. No credential of any kind.

---

## What the guarantee actually is

There are two ways to claim a run stayed local, and they are not equally strong.

**The weak one:** *the cloud routes have no key, so they are filtered out.* That
is a filter, and a filter holds only as long as every future code path remembers
to apply it. A chain entry, a catalogue file, a `refresh_chains()` or a pinned
model name could each put a request back on the wire.

**The one this implements:** under `OE_MAX_LOCAL_ONLY` the commercial adapters
are **never constructed**. `build_default_registry()` returns only the local
providers, so there is no object holding a remote URL, nothing to filter, and no
code path that can dial one by accident.

`tests/oe_max/test_local_mode.py` asserts the strong claim, including that every
provider in the registry points at `127.0.0.1`.

## Supported servers

All four speak the OpenAI protocol, which is the only thing the broker needs, so
none required a new adapter.

| Server | Default endpoint | Override |
|---|---|---|
| Ollama | `http://127.0.0.1:11434/v1` | `OE_MAX_OLLAMA_BASE` |
| LM Studio | `http://127.0.0.1:1234/v1` | `OE_MAX_LMSTUDIO_BASE` |
| vLLM | `http://127.0.0.1:8000/v1` | `OE_MAX_VLLM_BASE` |
| llama.cpp (`llama-server`) | `http://127.0.0.1:8080/v1` | `OE_MAX_LLAMACPP_BASE` |

You do not need all four. A server that is not running lists nothing and
contributes no routes — the same shape as a provider whose credential is absent
— so running just Ollama is a supported configuration, not a degraded one.

Pointing an override at another machine (`OE_MAX_VLLM_BASE=http://gpu-box.lan:8000/v1`)
is supported. It is not a contradiction of local mode: "local" here means no
route to a commercial provider this project configured, not no sockets.

## Model ids are discovered, never configured

There is no model name anywhere in the local configuration, and that is
deliberate. What your machine serves is whatever you pulled, and a shipped guess
like `llama3.1` would be exactly the kind of remembered id that has already
bitten this project three times (see `CLAUDE.md` rule 6).

The broker reads `/v1/models` from each local server at startup and materialises
whatever it finds. Embedding, reranking and vision models are dropped — they
answer a different API shape, and routing a mutation to an embedding model
produces a confusing failure rather than an obvious one.

Because ids are only knowable by asking, the broker **discovers at startup** in
local-only mode rather than waiting for `--verify`. Serving before discovering
would mean an empty chain and a failure on every request.

### Saying which model you want first

Discovery gives you the server's listing order, which is not a ranking. On a
machine with a tuned 27B and eleven experiments beside it, the chain led with
whichever Ollama happened to list first — a 1 GB experiment, in the case that
prompted this.

Nothing in this repository ranks local models. A machine's best model depends on
its hardware and what its operator pulled, and inventing an order would read as
a measurement nobody took. But *you* know, so say so:

```bash
export OE_MAX_LOCAL_MODELS="qwen-evo-text,qwen3:0.6b"
```

```powershell
$env:OE_MAX_LOCAL_MODELS = "qwen-evo-text,qwen3:0.6b"
```

Comma-separated, most preferred first. Each entry is a **substring** of the model
id, matched case-insensitively — not a regular expression, so `qwen3:0.6b` means
what it looks like rather than treating the dot as a wildcard.

Measured on a box with fifteen models listed:

| | first four routes |
|---|---|
| unset | `qwen-evo-local-3b-v1`, `qwen2.5:3b`, `qwen-evo-local-qwen3-tiny-v1`, … |
| `qwen-evo-text,qwen3:0.6b` | `qwen-evo-text`, `qwen3:0.6b`, `qwen-evo-local-3b-v1`, `qwen2.5:3b` |

Two properties worth relying on:

* **Unset changes nothing.** The default is the listing order it always was.
* **Naming a model you have since deleted does not empty the chain.** Everything
  you did not name stays routable, in the server's own order, behind everything
  you did. A chain that can empty itself is worse than one ordered imperfectly.

## Fitting a model to your card

The settings below are defaults. Getting a *specific* model onto a *specific* GPU is its own exercise, and **[local-tuning.md](local-tuning.md)**
works through a real one end to end — a 27B model on an 8 GB card, what
each change was worth in measured tokens per second, and the silent
failure that cost the most (an empty answer, HTTP 200, no error anywhere).

## Settings that differ from a cloud run

`configs/oe_max/local.yaml` is not the cloud config with a different URL. Three
things genuinely change:

- **`timeout: 1800`, and a 1800s provider timeout.** A 30B model on CPU can
  spend minutes on one mutation and be working correctly. A cloud-sized ceiling
  manufactures timeouts and then blames the model.
- **`max_tokens: 4096`.** A local model generates at a fraction of a hosted
  one's rate, so a 16,000-token budget is minutes of wall clock per mutation.
  Raise it if your model reasons before answering — but raise the timeouts with
  it, never alone (`project/handoff.md` §3.3).
- **`parallel_evaluations: 1`, smaller population.** Two workers against one
  local server queue at the model rather than running in parallel, and on a
  saturated GPU they compete for the same memory.

## Both routing layers, not just one

There are two model routers in this repository and the switch has to cover both,
because they serve different callers:

| Layer | Used by | Local-only behaviour |
|---|---|---|
| `oe_max/providers/registry.py` | the broker, i.e. **evolution** | only the four local adapters are constructed |
| `control_plane/providers/profiles.py` | `ModelRouter`, i.e. the **agent** | only local profiles are constructed, every role |

Covering only the first was the state this landed in first, and it is worse than
covering neither: the switch reads as covering everything, so an agent quietly
reaching a commercial endpoint while the run beside it could not would be very
hard to notice.

## The agent, and its tools

`control_plane/agent/` is a tool-using runtime: a goal is decomposed into tasks,
each task is a conversation in an isolated execution world, and every tool call
and result is a typed event so the run replays from its log.

The tools it exposes to a model:

| | |
|---|---|
| files | `read_file`, `write_file`, `file_metadata`, `glob`, `search_text` |
| shell | `shell` (bounded by timeout, inside the world) |
| git | `git_status`, `git_diff_stat`, and worktree isolation per candidate |
| processes | `process_start`, `process_read`, `process_write`, `process_wait`, `process_terminate` |

The process tools are the ones worth calling out: they run a *persistent*
program without a shell, so a model can start a server or a REPL, read its
output incrementally, write to its stdin and terminate it with its children —
rather than being limited to one-shot commands.

In local-only mode every role — including the tool-requiring ones
(`orchestrator`, `planning`, `review`, `architecture`) — routes to a local
server. The local profiles declare `TOOLS`, which is a claim about the *server*:
all four implement the OpenAI tools API, while whether a given model honours it
varies. The doctor probes and can withdraw the capability from measurement. The
alternative — declaring `CHAT` only — was tried first and leaves every
tool-requiring role with no route at all, so the agent cannot run locally under
any model.

`OpenEvolve` gets `run_native_model_agent()` and friends from
`control_plane.native.install()`, which binds them at runtime. They are
deliberately *not* methods on upstream's controller — see `patch-surface.md`.

## Scientific tools, also local

`control_plane/scientific/` routes structured problems to whatever computation
backends are installed on this machine — SymPy, NumPy, SciPy, NetworkX, and
optionally JAX, CVXPY, OR-Tools, QuTiP, Astropy, OpenMM, PyMatGen, plus Z3, Lean
and Sage if they are on `PATH`.

```bash
uv pip install -e ".[scientific]"      # sympy, scipy, networkx
curl http://127.0.0.1:8000/api/scientific/capabilities
```

Two properties worth knowing:

**Availability is measured, not declared.** A backend that is not installed is
reported `unavailable` *with the reason*. It is never omitted — the caller would
not know it could have helped — and never faked into a result.

**Results carry a verification status, not a boolean.** There is no `SUCCESS` in
the vocabulary. `numerically_supported` and `symbolically_verified` are
different claims, and a float that satisfies an equation to 1e-12 has not been
proved. Collapsing those is how a search starts trusting a number it should not.

A request naming a capability nothing installed provides returns `inconclusive`
and says what would have served it, rather than being handed to an unrelated
backend.

## What has been verified, and what has not

**Verified end to end, 2026-08-28.** A broker started with `OE_MAX_LOCAL_ONLY=1`
discovered a local OpenAI-compatible server on this machine, and a 6-iteration
evolution completed — 6 requests served, 0 failed, every
one routed to the local provider, with only the four local adapters present in
the process.

**Not verified: a real local LLM.** That run used
`scripts/local_provider.py`, which is a genuine OpenAI-compatible HTTP server
but a deterministic generator rather than a model. No Ollama, LM Studio, vLLM or
llama.cpp instance was available on this machine, so **no local model has
actually generated a mutation here**.

What that means precisely: the plumbing — discovery, model materialisation,
chain construction, routing, the offline guarantee, and a complete evolution
cycle — is verified. Whether a given 7B model produces *useful* mutations is a
property of that model, and you will find out on your first run. Expect to tune
`max_tokens` and the timeouts; a small model that reasons before answering hits
the same truncation failure documented in `project/handoff.md` §3.2.
