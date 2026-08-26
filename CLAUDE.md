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

4. **Never claim a live test passed if it did not run.** NVIDIA NIM and
   OpenRouter are unverified in this repo and labelled as such everywhere. Keep
   that discipline.

5. **Credentials stay inside the broker process.** Candidates and evaluators get
   no keys. Redaction runs before persistence, not at render time.

6. **Provider and model IDs are configuration, not constants.** Ox Alpha was a
   stealth preview that may vanish — and on 2026-08-26 it did, taking three
   other configured routes with it while the whole test suite stayed green.
   Run the provider doctor before trusting the routing table: NEXT_TASKS T0,
   HANDOFF §3.11. Never write a model id from memory into a profile; take it
   from the provider's `/models` listing.

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
./test.sh     # 437 upstream + 362 control plane + 258 OE-MAX
```

The upstream suite runs first and is the regression gate: if it breaks, the
fork is broken, not merely the control plane.

## Traps that will cost you an hour

- `urllib` gets Cloudflare `403 error code: 1010` from OpenCode Zen; `httpx`
  works. (Fixed in the doctor — do not reintroduce it in new probing code.)
- Every route in the table is a reasoning model and can spend its whole budget
  on *hidden reasoning* before the first visible token; `max_tokens`, the
  provider timeout and the client timeout must be changed together. A too-small
  budget produces an empty HTTP 200, which is easy to mistake for a healthy one.
- A model in Zen's `/models` listing may still return "Model is unavailable" —
  and a model absent from the listing may still serve. The catalogue is
  evidence, never a gate.
- A tools request returning HTTP 200 proves nothing. Check for a `tool_calls`
  entry, and check it more than once: `laguna-s-2.1-free` emits one on roughly
  1 attempt in 3.
- The telemetry bus must be rebuilt after `fork()`, or all worker-process
  telemetry silently disappears.
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
control_plane/  telemetry, storage, APIs, sandbox isolation, runner
web/            Control Center (React + TS)
scripts/        operator scripts (.sh and .ps1)
tests/          upstream (untouched) + tests/evolution + tests/oe_max
```
