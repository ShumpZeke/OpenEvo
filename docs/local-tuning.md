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

Measured across the three cache types:

| `OLLAMA_KV_CACHE_TYPE` | resident | prompt/s | generation |
|---|---|---|---|
| **q8_0** | 39% | **74.3** | 3.35 tok/s |
| q4_0 | 39% | 74.3 | 3.41 tok/s |
| f16 (the default) | 34% | 14.7 | 2.98 tok/s |

Two things to take from that. The default f16 cache is **5× slower on prompt
evaluation**, which is most of the gain usually credited to flash attention
alone — the two are set together and it is the quantised cache doing the heavy
lifting. And `q4_0` buys essentially nothing over `q8_0`: identical prompt rate,
a generation difference inside the noise, and the same 39% resident. Since a
smaller cache costs attention precision, there is no reason to take it. **Stop
at q8_0.**

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

### 5. Use every logical core, not just the physical ones

The standard advice is that hyperthreads hurt inference, because two logical
cores share one physical core's vector units. Measured here it is the reverse,
and monotonically:

| `num_thread` | prompt/s | generation |
|---|---|---|
| 4 | 64.6 | 3.14 tok/s |
| 6 (Ollama's default) | 63.8 | 3.26 tok/s |
| 8 | 69.6 | 3.38 tok/s |
| **12** | **72.4** | **3.41 tok/s** |

About +4.6% generation and +13% prompt over the default, for a config line.

The advice is not wrong in general — it is written for workloads that saturate
the vector units. Roughly 60% of this model runs on the CPU and is
memory-bandwidth bound, so threads spend much of their time stalled on memory,
and a stalled thread leaves units its sibling can use. Which regime you are in
is a property of the model/hardware ratio, not of hyperthreading, so it is worth
the five minutes to check rather than inherit.

### 5b. Do NOT force the GPU layer count

The most counterintuitive result here, and worth the space. Ollama picks a
GPU/CPU split automatically. Overriding it with `num_gpu` was **monotonically
worse at every step**:

| `num_gpu` | resident | generation | vs auto |
|---|---|---|---|
| auto | 39% | **3.26 tok/s** | — |
| 36 | 54% | 2.56 tok/s | −21% |
| 40 | 60% | 2.08 tok/s | −36% |
| 44 | — | 1.65 tok/s | −49% |
| 48 | — | 1.39 tok/s | −57% |

Prompt evaluation collapses alongside it, 68.9 → ~20 tok/s.

"More layers on the GPU" stops being true past the point where they fit. On
Windows the driver will happily place the overflow in **shared GPU memory** —
system RAM addressed across PCIe — and a layer there is slower than the same
layer running natively on the CPU. Ollama's heuristic already accounts for the
KV cache and the compute buffers, which the naive
`VRAM ÷ bytes-per-layer` arithmetic does not.

`num_ctx` 4096 measured identical to 8192 (3.25 vs 3.26 tok/s), so context is
not the binding constraint here either. There is nothing to buy by shrinking it,
and 8192 comfortably holds a mutation prompt.

**If your runtime has no auto-split** — llama.cpp defaults `-ngl` to 0 — start
low and raise it a few at a time, keeping the value where throughput stops
improving. A server that dies during load is well past the limit; one that
merely got slower is just past it, which is harder to notice and is why this
wants measuring rather than reasoning.

---

## Optimise applicable diffs, not tokens per second

The single most valuable measurement here, and it went the opposite way to the
optimisation that produced it.

Generation costs ~0.3 s per token, so shortening the *answer* looked like the
obvious lever. The real lever turned out to be constraining it.

Measured through `PromptSampler.build_prompt` — the same call the engine makes —
varying only `system_message`, four runs per arm:

| `system_message` | mean tokens | mean seconds | **applicable diffs** |
|---|---|---|---|
| plain instruction | 1396 | 477 | **1 of 4** |
| explicit output rules | 400 | 145 | **4 of 4** |

Per *usable* diff that is roughly **32 minutes against 2.4 minutes — about 13×**.

The mechanism is in the raw numbers: the plain arm hit the 1536-token ceiling in
three of four samples. It rambles, gets truncated mid-diff, and a truncated
SEARCH block cannot match anything. The model is not bad at diffs; nothing told
it to stop talking. Raising `max_tokens` would not fix that — it would buy a
longer ramble at 0.3 s per token.

### How this was measured wrong the first time

Worth recording, because the first version of this table was published and was
not comparable to a real run.

Both prompts were hand-written, and only one carried a SEARCH/REPLACE format
spec. But OpenEvolve's `diff_user` template already supplies that spec **and a
worked example** to every prompt it sends, so the "baseline" arm was a string
the engine would never produce. It measured a straw man.

Two lessons, both cheap to apply. Build the prompt with the code that builds
prompts, rather than approximating it. And when an A/B needs one arm to be
"realistic", check what the system actually sends before assuming the harness
reproduces it — the numbers were plausible, internally consistent, and wrong.

The line that fixes it is the one demanding the *smallest unique* SEARCH span,
copied exactly. It costs tokens and buys diffs that apply.

So the metric to tune is **applicable diffs per hour**, not tokens per second.
On these numbers the fast prompt scores zero at any speed, and that is not a
narrow margin that better sampling would close.

This is the same distinction the project already draws between raw and useful
yield, arriving from a different direction.

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

### On a real mutation prompt, it is the difference between output and none

The measurement above used a one-word probe. Repeated on a full prompt built by
the engine's own sampler, on the 27B, once each:

| `reasoning_effort` | wall | completion | `content` | diff |
|---|---|---|---|---|
| absent | 308.7 s | 978 tok | **0 chars** (3247 chars of reasoning) | none |
| `"none"` | **99.3 s** | 320 tok | 1029 chars | applicable |

Three times the wall clock for nothing usable. `finish_reason` is `stop` both
times, so it does not read as truncation and raising `max_tokens` does not help.
This is the largest single lever measured on this box — larger than every
Ollama setting in this document put together.

### Set it in the config as well as the adapter

The broker's local adapter has always sent it, so a run through
`127.0.0.1:8787` was fine. But `api_base` is a config field, and pointing it at
`http://127.0.0.1:11434/v1` — the obvious move when you want the broker out of
the picture — takes the adapter out of the path along with its setting. The
symptom is every iteration failing with **"No valid diffs found in response"**,
minutes apart, while the model works perfectly.

`configs/oe_max/local.yaml` now sets `llm.reasoning_effort: "none"` too, so
neither path can lose it. Keep both halves; a test fails if either is removed.

---

## Prompt processing is cached, and the prompt defeats the cache

Re-sending a prefix the server has already processed is close to free. Measured
on the tuned 27B, a 2078-token prompt sent cold and then again with a different
tail, on two independent prefixes:

| | prompt processing |
|---|---|
| cold | 15.0 s / 15.1 s |
| prefix already seen | 3.6 s / 3.5 s |

**4×**, consistent across both. A KV cache is a *prefix* cache, though: one
changed token near the beginning throws away everything after it.

Upstream's `diff_user` template opens with

```
# Current Program Information
- Fitness: {fitness_score}
```

so consecutive prompts diverge at **character 44**, and the ~1100-character
format specification — which never changes — is re-processed every iteration.
Measured across four consecutive rounds with the program and metrics changing as
they would in a run, only 591 characters of a 3938-character prompt are a
reusable prefix.

Reordering the invariant block to the front takes that to 1609 characters
(15% → 41%), worth about **1.4 s per call**.

That is ~1% of a 145 s call, because prompt processing is only ~5% of the cost
here and generation dominates — so it is filed as a measured hypothesis rather
than shipped. Moving the output-format spec from the end of the prompt to the
beginning may change how often the model emits an applicable diff, and one
wasted call is 145 s, a hundred times the saving. The reordered template and the
full numbers are in `configs/local/prompts/`; it is not wired into any config
until that quality question is answered.

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

## Leave `use_mmap` alone

Tried and abandoned. With mmap on (the default) the OS maps the 14 GB file and
pages it, so the ~10 GB of CPU-resident layers can in principle be evicted and
faulted back mid-inference. Turning it off loads everything up front, which
removes that risk — if it fits.

It does not fit here, and it does not fail cleanly. Free RAM went to **0.1 GB**
and the machine began thrashing before the first measurement completed; the test
was killed rather than finished. A 14 GB model on 16 GB of RAM has no headroom
for a resident copy once the OS, the GPU driver and everything else are counted.

No number for this one — the experiment was abandoned, not completed, and that
is the honest state. Worth knowing only so nobody repeats it expecting a quick
win.

## Do not sweep-verify a local setup

`--verify` smoke-tests every configured model, which locally means loading each
one. Five discovered routes took over ten minutes here and drove free RAM to
0.3 GB. Startup discovery — the default — only reads `/v1/models`, is instant,
and is enough to route. See [gotchas.md](gotchas.md).

## Model size: what the small model buys, and what it does not

The largest lever on this box is not a setting. It is which model runs.

### Speed

Measured on the same task, prompts built by OpenEvolve's own sampler:

| model | generation | per call | applicable diffs |
|---|---|---|---|
| Qwen3.5-27B (tuned) | 3.3 tok/s | ~145 s | 4 of 4 |
| `qwen3:0.6b` | **192.6 tok/s** | **0.5 s** | 6 of 12 |
| `qwen2.5:1.5b` | 137.6 tok/s | 2.4 s | 0 of 12 |
| `qwen2.5:0.5b` derivative | 267.6 tok/s | 1.7 s | 0 of 12 |

Format ability does not track size. `qwen3:0.6b` writes an applicable
SEARCH/REPLACE diff half the time; `qwen2.5:1.5b`, more than twice the size,
managed none in twelve attempts. Worth checking before assuming a bigger local
model behaves better.

### Quality

Now measurable, because `benchmarks/tasks/fn_min_seeded` fixes the random draws
— see that directory's README. Same task, same config apart from the model.

**The 27B run did not finish.** It was configured for thirty iterations and
stopped after **19**. Its best was found at iteration 6 and re-scored
independently, so finishing could only have improved its number, not weakened
the comparison — but the row is 19 iterations, not 30, and the 0.6B row is a
completed 30.

It stopped because the machine ran out of memory: starting the broker alongside
a run holding a 16 GB model left 0.2 GB free, Ollama's keep-alive expired, and
`llama-server` sat thrashing trying to reload a model that no longer fit. Worth
knowing as an operating constraint on a 16 GB box — one 27B run is the whole
machine, and anything else started beside it is what ends it.

| | best program | score |
|---|---|---|
| seed program | random search, budget 1000 | 1.4061 |
| `qwen3:0.6b`, 30 iterations, **3 minutes** | the same random search, budget **2000** | 1.4513 |
| Qwen3.5-27B, best at iteration 6 of 19 run | **Differential Evolution**, adaptive F/CR, budget 1200 | **1.4987** |

Both improvements are real and reproduce exactly on re-scoring. They are not the
same kind of thing.

The 0.6B's entire gain came from doubling the search budget — the task scores no
runtime, so that is free score for no insight. The 27B rewrote the algorithm.
Holding the budget equal separates them:

| | combined | value | distance |
|---|---|---|---|
| seed, budget 1000 | 1.4061 | 0.9692 | 0.8425 |
| 0.6B's answer (budget 2000) | 1.4513 | 0.9895 | 0.9092 |
| **27B's answer at budget 1000** | **1.4962** | 0.9995 | 0.9923 |
| 27B's answer at its own budget 1200 | 1.4987 | 0.9997 | 0.9977 |

At the same budget the algorithm is worth **+0.090**; raising 1000 to 1200 adds
0.0025. So the 27B's improvement is almost entirely the algorithm, and it is
twice the size of everything the 0.6B found.

### Given ten times the iterations, the small model still found nothing

That was the obvious objection to the table above — thirty iterations is not
many — so it was run. **300 iterations of `qwen3:0.6b`, about thirty minutes,
zero new bests.** Three distinct scores across the whole run: 0.0 for candidates
that would not evaluate, 0.1817 once, and 1.4061, the seed's own score, for
everything else. The final best differs from the seed textually and scores
identically, which is the signature of edits that change code without changing
behaviour.

So the earlier thirty-iteration run that found `iterations=2000` was a lucky
draw, not a capability. Ten times the sampling did not reproduce it.

One confounder had to be eliminated first, because the two 0.6B runs differed by
more than length: the later one had `reasoning_effort: "none"`, added to the
config after the 27B measurement. Thinking might be exactly what a small model
needs to produce a well-formed diff, in which case the null result would be an
artefact of a setting chosen for a much larger model. Measured on the 0.6B,
n=16 each:

| `reasoning_effort` | applicable | per call | output | per usable diff |
|---|---|---|---|---|
| absent (thinks) | 8/16 | 3.87 s | 762 tok | 7.7 s |
| `"none"` | 7/16 | **0.61 s** | 100 tok | **1.4 s** |

8 against 7 out of 16 is noise; 6.3× the speed is not. So the setting costs the
small model nothing in diff quality and buys it a great deal of throughput —
it is right for both sizes, and it does not explain the null result.

### What that means for going faster

The 0.6B is genuinely usable for exercising the machinery: it produces
applicable diffs about half the time, drives the loop end to end, and a full
run costs three minutes. That is worth a lot when the thing being debugged is a
pipeline.

It has not, in 330 iterations across two runs, produced an improvement that
survived. The 27B produced one at iteration 6.

So on this task the small model is a development instrument and the large one is
the tool. Which is only sayable because the evaluator stopped moving — the
unseeded task's spread is 0.39, and every difference discussed above is smaller
than that.

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
