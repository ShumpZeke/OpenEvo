# Working in this repository

Read `HANDOFF.md` before making changes. `NEXT_TASKS.md` has the prioritised
queue. This file is the short version of the rules that are load-bearing.

## Hard rules

1. **Never edit `openevolve/`.** It is byte-identical to upstream `411fb59c`,
   which is what makes upstream merges fast-forward. Add behaviour by wrapping
   public methods at runtime — `control_plane/telemetry/instrument.py` is the
   pattern. If you genuinely must edit it, record it in `PATCH_SURFACE.md` with
   the reason.

2. **No fabricated data in the UI.** There are no fixtures in `web/` and there
   must not be. If the backend has no value, render "no data" — never a zero, a
   placeholder, or a plausible-looking number.

3. **Unsupported controls are disabled with a reason**, never rendered as
   buttons that do nothing. See `RunManager.CAPABILITIES`.

4. **Never claim a live test passed if it did not run.** OpenRouter and the 13
   catalogue providers are unverified for inference in this repo and labelled
   as such everywhere. NVIDIA NIM is no longer in that set — it was verified
   with a real key on 2026-08-28 and 5 of its 9 configured ids serve
   (HANDOFF §4i). Keep the discipline in both directions: a label that is stale
   in the pessimistic direction is as wrong as one that overclaims.

5. **Credentials stay inside the broker process.** Candidates and evaluators get
   no keys. Redaction runs before persistence, not at render time.

6. **Provider and model IDs are configuration, not constants.** A stealth
   preview this repo used as its primary vanished on 2026-08-26, and two NVIDIA
   models it shipped as its "strong fallback" were never in NVIDIA's catalogue
   at all. Model ids are discovered from provider listings and reconciled on
   every discovery — do not write one down from memory. The cleanest evidence:
   `nvidia/nemotron-nano-3-30b-a3b` 404s while `nvidia/nemotron-3-nano-30b-a3b`
   serves, and both are listed.

7. **NVIDIA NIM is the primary provider** (operator decision, 2026-08-27). It
   leads every role chain in `oe_max/roles.py` and
   `control_plane/providers/profiles.py`. Ox Alpha is removed from service —
   not disabled, removed — and `tests/oe_max/test_roles.py` plus
   `tests/evolution/test_providers.py` fail if either fact regresses. Leading
   with a key-gated provider is only safe because a route whose credential is
   absent is filtered out rather than attempted: keep the keyless Zen tail in
   every chain, or a checkout with no key has nothing to serve from.

## If another agent is working here too

More than one agent (Codex, another Claude session) may be committing to this
repo concurrently. Cheap discipline that avoids clobbering:

```bash
git pull --rebase origin main    # ALWAYS, before you push
./test.sh                        # re-run after the rebase, not just before
git push origin HEAD:main
```

Rebase rather than merge, so history stays linear and a conflict shows up as a
conflict instead of a silent merge that drops someone's change. Keep commits
small and focused on one subsystem — two agents editing `oe_max/router.py` and
`web/src/views/` respectively will never conflict; two agents both "improving
things broadly" will.

If you find commits you did not write, read them before building on top. The
other agent may already have fixed the thing you were about to fix.

## Before pushing

```bash
./test.sh     # 431 upstream + 456 control plane + 359 OE-MAX + 34 BrainPort
              # 6 upstream tests fail on Windows only (platform, not regression)
              # -- see TEST_STRATEGY.md; do NOT 'fix' them by editing openevolve/
```

The upstream suite runs first and is the regression gate: if it breaks, the
fork is broken, not merely the control plane.

## Traps that will cost you an hour

- `urllib` gets Cloudflare `403 error code: 1010` from OpenCode Zen; `httpx`
  works. (Fixed in the doctor — do not reintroduce it in new probing code.)
- Reasoning models spend most of a small budget on *hidden reasoning*;
  `max_tokens`, the provider timeout and the client timeout must be changed
  together. Only `laguna-s-2.1-free` measured zero, which is why it judges.
- A 429 saying "Rate limit exceeded, please try again later" may mean the free
  allowance is spent, not that you are too fast. Only the error type says which.
- A model in Zen's `/models` listing may still return "Model is unavailable".
- The telemetry bus must be rebuilt after `fork()`, or all worker-process
  telemetry silently disappears. Under `spawn` — the default on Windows and
  macOS — there is a second, separate way to lose it: the pool initializer is
  pickled by reference and re-resolves to upstream's unwrapped function in the
  child. Both have the same symptom, `model_requests: 0` on a run that is
  plainly working. See HANDOFF §3.5 and §3.5b.
- The rate limiter's epsilon is not cosmetic — removing it makes `acquire()`
  spin forever, and the tests hang rather than fail.
- Nothing in memory crosses the worker→main process boundary. Anything a worker
  must tell the main process rides on `Program.metadata` — and `_migrate_programs`
  copies metadata wholesale, so exclude migrants or you will double-count.
- Through the broker every route is called `oe-max-primary`. The serving route
  comes from the `oe_max` stamp on the response body, not from what was asked
  for.

Full detail for each: `HANDOFF.md` §3.

## Layout

```
openevolve/     upstream engine — DO NOT EDIT
oe_max/         provider broker, rate limiter, gates, search, archives,
                verification
oe_max/brain/   BrainPort — the OpenCode path. Its own evolution loop; shares
                nothing with the upstream engine. See HANDOFF.md §4h.
packages/       the OpenCode plugin (TypeScript); dist/ is built, not committed
control_plane/  telemetry, storage, APIs, sandbox isolation, runner
web/            Control Center (React + TS)
scripts/        operator scripts (.sh and .ps1)
tests/          upstream (untouched, root only) + tests/evolution
                + tests/oe_max + tests/brain
```
