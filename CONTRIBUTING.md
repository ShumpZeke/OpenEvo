# Contributing

This repository already carries most of what you need, in files that are kept
current rather than written once. This page routes you to the right one and
states the handful of rules that are load-bearing.

## Start here

```bash
git clone https://github.com/ShumpZeke/OpenEvo.git
cd OpenEvo
./bootstrap.sh          # Windows: .\bootstrap.ps1
./test.sh               # Windows: .\test.ps1
```

`bootstrap` creates `.venv`, installs the engine and control plane with the
`[dev]` extra, initialises storage, builds the Control Center and the OpenCode
plugin, and runs a smoke test. It touches nothing outside this directory.

Then read, in order:

| | |
|---|---|
| [CLAUDE.md](CLAUDE.md) | the short version of the rules — read before your first change |
| [HANDOFF.md](HANDOFF.md) | what is true right now, and §3, the traps |
| [NEXT_TASKS.md](NEXT_TASKS.md) | the prioritised queue, with rationale and counter-arguments |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the pieces fit |
| [TEST_STRATEGY.md](TEST_STRATEGY.md) | what is tested, what is not, and the six Windows failures |

## The rules that matter

**1. Never edit `openevolve/`.** It is byte-identical to upstream `411fb59c`,
which is what makes an upstream merge a fast-forward instead of a conflict
resolution. Add behaviour by wrapping public methods at runtime —
`control_plane/telemetry/instrument.py` is the pattern, and
`tests/evolution/test_patch_surface.py` enforces the invariant. If you genuinely
must edit it, record the reason in [PATCH_SURFACE.md](PATCH_SURFACE.md).

**2. No fabricated data in the UI.** There are no fixtures in `web/` and there
must not be. Where the backend has no value, render "no data" — never a zero, a
placeholder, or a plausible-looking number.

**3. Unsupported controls are disabled with a reason**, never rendered as
buttons that do nothing. See `RunManager.CAPABILITIES`.

**4. Never claim a live test passed if it did not run.** Several integrations in
this repo are labelled UNVERIFIED on purpose. That label is information; keep it
accurate in both directions.

**5. Credentials stay inside the broker process.** Candidates and evaluators get
no keys, and redaction runs before persistence rather than at render time.

**6. Provider and model IDs are configuration, not constants** — they are
discovered from each provider's own listing. A model in a `/models` response is
not necessarily a working model, which is why discovery is list-then-smoke-test.

## Evidence

The project's habit is that a claim carries a number or carries the word
"unverified". A run against the bundled local provider is a smoke test, not a
result: it replays a fixed pool of diffs and never reads the prompt, so it
cannot show an effect for anything prompt-dependent. Say which provider produced
a measurement and how many attempts it rests on.

## Before you open a pull request

```bash
git pull --rebase origin main
./test.sh                          # re-run after the rebase, not just before
```

The upstream suite runs first and is the regression gate: if it breaks, the fork
is broken, not merely the control plane. Six upstream tests fail on Windows for
platform reasons in upstream's own code — enumerated with their causes in
[TEST_STRATEGY.md](TEST_STRATEGY.md). Do not "fix" them by editing
`openevolve/`. CI runs the upstream suite on Linux, where it is green.

Keep commits small and scoped to one subsystem. More than one agent or person
may be committing here concurrently; rebase rather than merge so history stays
linear and a conflict surfaces as a conflict.

## Licence

Apache-2.0, inherited from OpenEvolve. Upstream retains copyright over
`openevolve/`, `scripts/visualizer.py`, `configs/` and `examples/`. By
contributing you agree your contribution is licensed under the same terms.
