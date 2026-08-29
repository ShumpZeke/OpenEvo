# Tuning a local model to your machine

Worked example on a real, constrained box. Every number here was measured, not
estimated, and the method matters more than the numbers — yours will differ.

**The machine:** Intel i5-12400F (6 cores / 12 threads), RTX 3050 **8 GB**,
16 GB system RAM, Windows 11.
**The model:** `Qwen3.8-27B-Uncensored:iq4_xs` — 27.3B parameters, IQ4_XS,
**14.26 GB** of weights plus a 0.87 GB vision projector. 65 layers, GQA with
4 KV heads, native context 262,144.

The mismatch is the whole story: **14.26 GB of weights, 8 GB of VRAM.** No
setting makes that fit. Everything below is about making the part that does not
fit cost as little as possible.

---

## Start by measuring, not by guessing

```bash
curl http://127.0.0.1:11434/api/ps
```

`size_vram` against `size` is the number that decides everything else. On this
box, stock settings gave:

| | |
|---|---|
| total | 16.27 GB |
| in VRAM | 5.44 GB (**33%**) |
| in system RAM | 10.83 GB (67%) |
| free RAM afterwards | **1.7 GB** |

Two thirds of the model on the CPU, and the machine down to 1.7 GB free. That
is the configuration to improve.

## What actually moved the needle

Measured with a realistic ~320-token mutation prompt, not a two-word probe —
prompt evaluation is a different bottleneck from generation and a short probe
hides it.

| | stock | tuned | text-only |
|---|---|---|---|
| prompt eval | 1.07 tok/s | 98.6 tok/s | **106.8 tok/s** |
| generation | — | 2.99 tok/s | **3.27 tok/s** |
| VRAM | 5.44 GB (33%) | — | 6.27 GB (39%) |
| wall, same task | — | 357 s | **213 s** |

### 1. Flash attention and a quantised KV cache

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION","1","User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE","q8_0","User")
```

Restart Ollama afterwards or nothing changes. This is not a micro-optimisation
on a box this tight: every byte the KV cache does not use is a byte of weights
that stays on the GPU instead of being evicted to system RAM. VRAM in use went
from 5.44 GB to 6.27 GB — the same 8 GB card holding measurably more model.

### 2. Drop what the workload cannot use

The packaged model carries a 0.87 GB CLIP vision projector. OpenEvolve mutates
source code and never sends an image, so that is memory spent on a capability
this workload cannot reach. Building text-only —
`configs/local/Modelfile.qwen35-27b-code-textonly`, which names the weights blob
directly instead of inheriting the packaged model — bought **8% on prompt and 9%
on generation**, and finished the same task in 213 s instead of 357 s.

Restate `RENDERER` and `PARSER` when you do this. Naming a blob directly does
not inherit them, and without them the chat template is wrong in ways that look
like the model being bad at instructions.

### 3. One resident model, kept warm

```powershell
OLLAMA_MAX_LOADED_MODELS=1     # this box cannot hold two
OLLAMA_NUM_PARALLEL=1          # parallel requests queue at the model anyway
OLLAMA_KEEP_ALIVE=30m          # a 20-46 s reload per mutation is not affordable
```

### 4. Sampling that suits code, not chat

The model ships `temperature 1` — Qwen's default for open-ended conversation.
Code mutation is not open-ended: a diff either applies or it does not, and a
high temperature buys creative failures. Qwen3's own non-thinking
recommendations are `temperature 0.7`, `top_p 0.8`, `top_k 20`.

Keep `repeat_penalty` at or below ~1.05. A SEARCH/REPLACE block restates
existing code *verbatim*; punish repetition harder and the restatement drifts,
the diff stops applying, and it looks like a bad mutation rather than a bad
setting.

---

## The one that produced no error at all

The most expensive problem here was silent. Through Ollama's OpenAI-compatible
endpoint:

```
request   max_tokens 400
response  content   ""
          reasoning "The user wants me to reply with exactly one word..."
          usage     completion_tokens 16
```

An empty answer, HTTP 200, no error anywhere. **It is not truncation** — a
400-token budget finished after 16. The model has a `thinking` capability, and
thinking is on by default; it thought, and then stopped.

`reasoning_effort: "none"` fixes it. The same request returns `READY`, and
latency through the broker fell from **8031 ms to 1695 ms**, because the
discarded thinking block is never generated. Local providers now send this by
default; set `OE_MAX_LOCAL_REASONING=low|medium|high` where you want thinking
back (a judge sometimes should think — a diff generator that thinks instead of
answering produces nothing to apply).

The broker also could not *see* this: it read reasoning tokens only from
OpenAI's `usage.completion_tokens_details`, and Ollama reports the text in
`message.reasoning` with no count. So the one number that would have explained
an empty answer said `reasoning_tokens: 0`. It now reads all three spellings of
the field and flags `answered_only_in_reasoning`.

---

## Sizing the token budget from the measured rate

This is where a sensible-looking default silently costs hours.

At **3.27 tok/s**, `max_tokens` is a wall-clock setting:

| budget | worst case per mutation |
|---|---|
| 16,384 (cloud default) | ~83 minutes |
| 4,096 | ~21 minutes |
| **1,536** | **~8 minutes** |

A SEARCH/REPLACE diff is typically 300–600 tokens, so `configs/oe_max/local.yaml`
uses 1,536: generous headroom for a large diff, capped before a ramble becomes
the run. Raise it only if you observe diffs actually being cut off. Raising it
"to be safe" costs wall clock on *every* iteration.

And never raise it alone — `max_tokens`, the provider timeout and the client
timeout are one coupled setting. See [gotchas.md](gotchas.md).

---

## One model file, three runtimes

You do not need three copies of a 14 GB file.

Ollama owns the blob. LM Studio reads it through a **hardlink** — same bytes,
second name, no extra disk and no administrator rights:

```powershell
cmd /c mklink /H `
  "$env:USERPROFILE\.lmstudio\models\<publisher>\<repo>\<model>.gguf" `
  "$env:USERPROFILE\.ollama\models\blobs\sha256-<digest>"
```

llama.cpp reads the same blob directly — `scripts/local/start-llamacpp.ps1`
points at it. Ollama's blobs are plain GGUF files named by hash; check with the
first four bytes, which read `GGUF`.

**Which runtime to prefer:** check what your llama.cpp build actually uses.

```bash
llama-server --list-devices
```

The winget `ggml.llamacpp` build on this machine reports `Vulkan0`, not CUDA.
Vulkan works on NVIDIA and is normally slower than CUDA, and Ollama uses CUDA —
so Ollama is the primary endpoint here and llama.cpp is the alternative. That is
a fact about a build, not about llama.cpp; a CUDA build would rank differently.

---

## Do not sweep-verify a local setup

`--verify` smoke-tests every configured model, which locally means loading each
one. Five discovered routes took over ten minutes here and drove free RAM to
0.3 GB. Startup discovery — the default — only reads `/v1/models`, is instant,
and is enough to route. See [gotchas.md](gotchas.md).

## Honest expectations

At ~3.3 tok/s generation and ~107 tok/s prompt, one mutation is roughly **3–8
minutes**. A 24-iteration run is therefore a couple of hours, not a coffee
break. That is what a 27B model on an 8 GB card costs, and no configuration
changes it.

If you want faster iteration rather than a larger model, the lever is the model:
something that fits **entirely** in 8 GB VRAM will be roughly an order of
magnitude quicker, because nothing runs on the CPU. That is a trade between
mutation quality and mutation rate, and the honest answer is that this
repository has not measured which wins — see the roadmap.
