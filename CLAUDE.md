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

6. **Provider and model IDs are configuration, not constants.** Ox Alpha is a
   stealth preview that may vanish.

## Before pushing

```bash
./test.sh     # 437 upstream + 84 control plane + 110 OE-MAX
```

The upstream suite runs first and is the regression gate: if it breaks, the
fork is broken, not merely the control plane.

## Traps that will cost you an hour

- `urllib` gets Cloudflare `403 error code: 1010` from OpenCode Zen; `httpx`
  works. (Fixed in the doctor — do not reintroduce it in new probing code.)
- Ox Alpha spends ~8,000 tokens on *hidden reasoning*; `max_tokens`, the
  provider timeout and the client timeout must be changed together.
- A model in Zen's `/models` listing may still return "Model is unavailable".
- The telemetry bus must be rebuilt after `fork()`, or all worker-process
  telemetry silently disappears.
- The rate limiter's epsilon is not cosmetic — removing it makes `acquire()`
  spin forever, and the tests hang rather than fail.

Full detail for each: `HANDOFF.md` §3.

## Layout

```
openevolve/     upstream engine — DO NOT EDIT
oe_max/         provider broker, rate limiter, gates, search, archives
control_plane/  telemetry, storage, APIs, sandbox isolation, runner
web/            Control Center (React + TS)
scripts/        operator scripts (.sh and .ps1)
tests/          upstream (untouched) + tests/evolution + tests/oe_max
```
