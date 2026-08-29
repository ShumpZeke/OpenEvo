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

## Status: measured for speed, UNVERIFIED for quality

1.4 s is about **1%** of a 145 s call on this box — prompt processing is only
~5% of the cost, and generation dominates. So the upside is small and certain,
while the risk is not:

Moving the instructions and the output-format specification from the end of the
prompt to the beginning puts them further from the model's most recent context.
Whether that changes how often the model emits an applicable SEARCH/REPLACE diff
has **not been measured**, and the applicable-diff rate matters far more than
1% — a single wasted call costs ~145 s, which is a hundred times the saving.

So this is kept as a measured hypothesis, not shipped. To settle it, compare the
applicable-diff rate under both templates on the same prompts, and wire it in
only if quality is neutral or better.
