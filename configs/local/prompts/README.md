# Prompt template overrides

`diff_user.txt` here is upstream's `openevolve/prompts/defaults/diff_user.txt`
with its blocks **reordered and not otherwise changed** — same sections, same
wording, so any measured difference is attributable to order alone.

**It is not wired into any config.** Nothing loads it until a config sets
`prompt.template_dir: configs/local/prompts`. See "Status" below for why.

## Why reordering could matter

A KV cache is a *prefix* cache: one changed token near the beginning throws away
everything after it.

Measured on `qwen-evo-text` (the tuned 27B), a 2078-token prompt sent twice:

| | prompt processing |
|---|---|
| cold | 15.0 s |
| same prefix, different tail | 3.5 s |

**4×**, reproduced on two independent prefixes (15.0→3.6 and 15.1→3.5), so
re-sending a prefix the server has already seen is close to free.

Upstream's template throws almost all of that away. Its first section is

```
# Current Program Information
- Fitness: {fitness_score}
```

so the prompt diverges between iterations at **character 44**, and everything
after it — including the ~1100-character format specification, which never
changes — is re-processed every single iteration.

Moving the invariant `# Task` block to the front changes the cacheable prefix,
measured across four consecutive rounds with the program and metrics changing as
they would in a run:

| template | prompt | cacheable prefix |
|---|---|---|
| upstream order | 3938 ch | 591 ch (15.0%) |
| this one | 3939 ch | 1609 ch (40.8%) |

That is +254 tokens of cache hit, worth roughly **1.4 s per call** at the
measured rates.

## Status: measured, and rejected

Not for the reason expected. Six calls per arm on the 27B, prompts built by the
engine's own sampler:

| template | applicable diffs | mean output |
|---|---|---|
| upstream order | 6/6 | 315 tok |
| this one | 4/4 (an HTTP 500 ended the arm at four) | **418 tok** |

**Diff quality: no difference detected, and no power to detect one.** Every call
in both arms produced an applicable diff. That rules out the failure that was
feared — moving the format specification away from the end of the prompt does
not stop the model following it — but a test where both arms sit at 100% cannot
distinguish a small regression from none.

**Output length: 33% longer.** That is the finding. Token counts are not
distorted by machine load the way the wall-clock figures in that run were, so
unlike the timings it is signal rather than noise.

At 3.4 tok/s, a hundred extra output tokens is about **30 seconds**. The reorder
saves **1.4**. Even allowing that n is 6 and 4 and that output length varies a
lot between calls, the difference would have to be wrong by a factor of twenty
before the trade paid.

So: the 4× prefix-cache effect is real, the prompt does defeat it, reordering
does recover most of it — and taking that back appears to cost twenty times what
it saves. Kept here as a recorded negative result, loaded by nothing.

If someone revisits this, the thing to measure is output length at higher n, not
the applicable rate.
