# Upstream Sync Strategy

## Current pin

```
repo    https://github.com/codelion/openevolve
commit  411fb59c886c18704caaffb611e17cf9e7d824d2
version 0.3.2   licence Apache-2.0
baseline 437 tests passing (17 slow deselected)
```

Recorded in `UPSTREAM.json`, and stamped into every run's provenance so a result
can be traced to the engine revision that produced it.

## Why merges are cheap here

`openevolve/`, `scripts/visualizer.py`, `configs/*.yaml`, `examples/` and
`tests/` are byte-identical to upstream. Telemetry is added by wrapping public
methods at runtime, not by editing call sites, so there is nothing to conflict.

The only file requiring manual reconciliation is `pyproject.toml`, because the
fork ships two packages.

## Procedure

```bash
# 1. Add upstream as a remote (once)
git remote add upstream https://github.com/codelion/openevolve
git fetch upstream

# 2. Capture the current baseline before changing anything
./test.sh 2>&1 | tee /tmp/baseline.txt

# 3. Take upstream's engine wholesale — there are no local edits to preserve
git checkout upstream/main -- openevolve/ scripts/visualizer.py \
    scripts/manual.py scripts/templates scripts/static configs/ examples/ tests/
#    (tests/evolution is ours and is not in upstream, so it survives)

# 4. Reconcile pyproject.toml by hand:
#    take upstream's [project.dependencies] additions,
#    keep our [project.scripts], [tool.setuptools.packages] and control-plane deps

# 5. Update the pin
python - <<'PY'
import json, subprocess, datetime
sha = subprocess.check_output(["git","rev-parse","upstream/main"], text=True).strip()
d = json.load(open("UPSTREAM.json"))
d["upstream_commit"] = sha
d["frozen_at"] = datetime.datetime.now().astimezone().isoformat()
json.dump(d, open("UPSTREAM.json","w"), indent=2)
PY

# 6. Verify
./test.sh
```

## What to check after a merge

The upstream suite passing is necessary but not sufficient — it does not
exercise our hooks. Also confirm:

**1. No hook lost its target.** A rename disables a hook with a warning rather
than raising, so grep for it:

```bash
./run.sh &   # start a short run, then:
grep -i "hook is disabled" .evolution/workspace/*/stderr.log
```

**2. Every event family still fires.** The control-plane tests assert this
against synthetic events; a live run is the real check:

```bash
curl -s "http://127.0.0.1:8000/api/query/runs/$RUN/events?limit=500" \
  | python3 -c "import json,sys,collections; print(collections.Counter(
      e['type'] for e in json.load(sys.stdin)['events']))"
```

A healthy short run produces `candidate.created`, `candidate.evaluation.*`,
`evaluator.*`, `model.request.*`, `map_elites.cell.updated`, `island.updated`,
`archive.updated`, `checkpoint.created`, `experiment.started/completed`, and —
on a run long enough to migrate — `island.migration.*`.

**3. Signatures the hooks depend on.** These are the load-bearing symbols:

| Symbol | Depended-on shape |
|---|---|
| `ProgramDatabase.add` | `(program, iteration=None, target_island=None)` |
| `ProgramDatabase.sample` | returns `(parent, inspirations)` |
| `ProgramDatabase.migrate_programs` | mutates `db.islands` in place |
| `ProgramDatabase.islands` / `island_feature_maps` / `programs` / `best_program_id` | read back after `add` |
| `ProgramDatabase._calculate_island_diversity` | optional; diversity omitted if gone |
| `Evaluator.evaluate_program` | `async (program_code, program_id="")` → metrics dict |
| `OpenAILLM.generate_with_context` | `async (system_message, messages, **kw)` |
| `OpenAILLM._call_api` | uses `self.client.chat.completions.create` |
| `OpenEvolve.run` / `._save_checkpoint(iteration)` | lifecycle + checkpoint path |
| `process_parallel._worker_init` | module-level, referenced at executor creation |
| `utils.metrics_utils.get_fitness_score` | fitness definition |

Private names (leading underscore) are the fragile ones. Each is either optional
or covered by a test that fails if its behaviour changes.

**4. Schema compatibility.** If upstream changes the checkpoint format, the
classic visualizer and `resume` are affected identically to upstream — we do not
wrap either. If upstream changes `Program` fields, projections may need a
migration; bump `SCHEMA_VERSION` and rebuild from the event log:

```python
Store(db).rebuild_projections_from_log("events.ndjson")
```

## Divergence policy

Prefer additive change, in this order:

1. new module under `control_plane/`
2. new runtime hook
3. new file alongside upstream (never a modification)
4. **editing an upstream file** — last resort; record it in
   [patch-surface.md](patch-surface.md) with the reason and the upstream issue
   or PR that would let the edit be dropped

The patch surface is currently empty. Keeping it that way is worth real effort;
every entry added is a conflict on every future release.
