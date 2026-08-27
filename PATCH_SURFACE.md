# Patch Surface

Every upstream file Evolution modifies. Required by SOURCE_OF_TRUTH section 27.

## Modified upstream files: **none**

```
$ diff -rq upstream-openevolve/openevolve/ openevolve/
(no differences)
```

`openevolve/`, `scripts/visualizer.py`, `scripts/manual.py`, `configs/*.yaml`,
`examples/` and every file in the **root** of `tests/` are byte-identical to
upstream at `411fb59c886c18704caaffb611e17cf9e7d824d2`. Re-run the check any
time:

```bash
git clone https://github.com/codelion/openevolve /tmp/up
git -C /tmp/up checkout 411fb59c886c18704caaffb611e17cf9e7d824d2
diff -rq /tmp/up/openevolve ./openevolve      # must print nothing
```

The fork's own tests live in `tests/evolution`, `tests/oe_max` and
`tests/brain` — never in `tests/` root, so the upstream suite keeps meaning
"upstream still works" rather than "our tests are green".

That has one consequence worth stating, because it is the reason a fork-side
test file was once renamed *upstream's* file out of the way: two test modules
with the same basename in directories that are not packages make pytest fail
collection with `import file mismatch`. The fork-side file gets renamed when
that happens. `tests/evolution/test_control_plane_api.py` is that rename —
upstream owns the name `test_api.py`.

## The surface is now enforced, not just documented

`tests/evolution/test_patch_surface.py` fails if anything under `openevolve/` is
modified **or added**. The added case is the one that motivated it: a new module
dropped into the engine tree conflicts with nothing on merge, so the fork keeps
merging cleanly right up until upstream adds a file at that path — and the empty
patch surface has silently not been empty for however long.

An upstream interface can always be implemented from outside the tree.
`oe_max/brain/llm.py` subclasses upstream's `LLMInterface` exactly as any
external package would, and lives in `oe_max/`.

## Why the surface is empty

Telemetry is normally added by editing call sites — a line in `database.add`,
another in `evaluator.evaluate_program`, and so on. That works, and it produces
a diff that conflicts on almost every upstream release, because those are the
exact functions upstream keeps changing.

Evolution instead wraps public methods at runtime
(`control_plane/telemetry/instrument.py`). Hooks are installed by the run
entrypoint before the engine starts, and each one calls straight through to the
original after recording what actually happened.

The trade is deliberate:

| | Edited call sites | Runtime wrapping |
|---|---|---|
| Upstream merge | conflicts on most releases | fast-forward |
| Breaks when… | upstream edits that line | upstream **renames the method** |
| Failure mode | merge conflict, caught at merge | hook disables itself, logged |
| Plain-CLI cost | always present | zero — no bus, no hooks |

A rename degrades telemetry rather than breaking evolution: `_patch()` logs a
warning and skips a hook whose target is missing. `tests/evolution/` fails loudly
if a hook stops producing its events, so a rename surfaces in CI rather than as
quietly missing data.

## Added files (new only, nothing overwritten)

```
control_plane/            control plane: telemetry, storage, API, providers,
                          sandbox, runner
web/                      Control Center frontend
tests/evolution/          control-plane tests (upstream tests untouched)
scripts/local_provider.py local OpenAI-compatible endpoint for offline testing
configs/evolution/        Evolution's own example configs
docs/                     architecture and operational documentation
bootstrap|run|dev|test    .sh and .ps1 entrypoints
pyproject.toml            replaces upstream's; keeps `openevolve-run` intact
UPSTREAM.json             pinned upstream ref + verified baseline
```

### `pyproject.toml` — the one replaced file

Upstream's `pyproject.toml` is replaced rather than modified, because the fork
ships two packages instead of one. The upstream console script is preserved
verbatim:

```toml
[project.scripts]
openevolve-run = "openevolve.cli:main"      # unchanged from upstream
```

Upstream's runtime dependencies are carried over unchanged and the control
plane's are appended. When merging an upstream release, reconcile this file by
hand — it is the only file that ever needs it.

## Runtime hooks

Each is a wrapper installed on a public method. Nothing on disk changes.

| Target | Records |
|---|---|
| `ProgramDatabase.add` | candidate created/rejected, MAP-Elites placement, archive, island rollup, best transitions |
| `ProgramDatabase.migrate_programs` | migration start/complete, per-island membership diff |
| `ProgramDatabase.sample` | parent + inspiration selection |
| `ProgramDatabase.load` | checkpoint loaded |
| `Evaluator.evaluate_program` | evaluation lifecycle, metrics, failures |
| `OpenAILLM.generate_with_context` | model request lifecycle, latency, errors, rate limits |
| `OpenAILLM._call_api` | token usage, stop reason, served model |
| `OpenEvolve.run` | experiment lifecycle + provenance |
| `OpenEvolve._save_checkpoint` | checkpoint created/failed |
| `process_parallel._worker_init` | installs the above inside pool workers |

State is read back from the live objects after the real call returns, so every
value is measured rather than predicted. `ProgramDatabase.add` is additionally
wrapped by the runner to service on-demand checkpoint requests at a safe
iteration boundary (`control_plane/runner/entrypoint.py`).
