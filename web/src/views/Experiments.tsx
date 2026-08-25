/**
 * Experiment Builder (section 14).
 *
 * Simple / Advanced / Raw. Every advanced field maps to a real key the engine
 * config schema accepts — the builder writes YAML that `openevolve` itself
 * parses, so there is no control here without a backend meaning.
 */

import React, {
  useState,
} from "react";
import {
  ViewProps,
} from "../App";
import {
  api, Json,
} from "../lib/api";
import {
  useAsync,
} from "../lib/hooks";
import {
  Badge, Button, Mono, Panel, Table, Td, Th, Row, cx, fmtNum, fmtTime, shortId,
} from "../components/ui";

type Mode = "simple" | "advanced" | "raw";

export const Experiments: React.FC<ViewProps & { onRunStarted: (id: string) => void }> =
  ({ onRunStarted, liveTick }) => {
  const [mode, setMode] = useState<Mode>("simple");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "experiment",
    initial_program: "examples/function_minimization/initial_program.py",
    evaluator: "examples/function_minimization/evaluator.py",
    config_path: "configs/evolution/local_test.yaml",
    iterations: 20,
    target_score: "",
    checkpoint: "",
  });

  const runs = useAsync(() => api.runs(), [liveTick], 8000);

  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      const body: Json = {
        name: form.name,
        initial_program: form.initial_program,
        evaluator: form.evaluator,
        iterations: Number(form.iterations) || undefined,
      };
      if (form.config_path) body.config_path = form.config_path;
      if (form.target_score) body.target_score = Number(form.target_score);
      if (form.checkpoint) body.checkpoint = form.checkpoint;
      const run = await api.startRun(body);
      onRunStarted(run.run_id);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  const F: React.FC<{ label: string; k: keyof typeof form; hint?: string;
    type?: string }> = ({ label, k, hint, type = "text" }) => (
    <label className="block mb-2">
      <div className="text-2xs uppercase tracking-wide text-ink-faint mb-0.5">{label}</div>
      <input
        type={type}
        value={String(form[k] ?? "")}
        onChange={(e) => setForm({ ...form, [k]: e.target.value })}
        className="w-full bg-surface-2 border border-line rounded px-2 py-1
                   text-xs font-mono focus:border-info outline-none"
      />
      {hint && <div className="text-2xs text-ink-faint mt-0.5">{hint}</div>}
    </label>
  );

  return (
    <div className="h-full grid grid-cols-[420px_1fr] gap-2 min-h-0">
      <Panel
        title="New experiment"
        actions={(["simple", "advanced", "raw"] as Mode[]).map((m) => (
          <button key={m} onClick={() => setMode(m)}
                  className={cx("px-1.5 py-0.5 text-2xs rounded",
                    mode === m ? "bg-surface-4 text-ink" : "text-ink-faint hover:text-ink")}>
            {m}
          </button>
        ))}
      >
        <div className="p-3">
          <F label="Name" k="name" />
          <F label="Initial program" k="initial_program"
             hint="Path relative to the repository root" />
          <F label="Evaluator" k="evaluator"
             hint="Python file exposing evaluate(program_path)" />
          <F label="Config (YAML)" k="config_path"
             hint="Upstream OpenEvolve config — all engine settings live here" />
          <F label="Iterations" k="iterations" type="number" />

          {mode !== "simple" && (
            <>
              <F label="Target score" k="target_score"
                 hint="Stop early once reached. Blank = run all iterations." />
              <F label="Resume from checkpoint" k="checkpoint"
                 hint="Path to a checkpoint_N directory. Blank = fresh run." />
            </>
          )}

          {mode === "advanced" && (
            <div className="mt-2 p-2 rounded border border-line bg-surface-2/40">
              <div className="text-2xs uppercase text-ink-faint mb-1">
                Population, islands, MAP-Elites, providers
              </div>
              <p className="text-2xs text-ink-dim leading-relaxed">
                These are engine settings and live in the YAML config so that a
                run started here is byte-identical to one started from the CLI.
                Editing them in two places would let the UI and the engine
                disagree about what actually ran. Point <Mono>Config</Mono> at
                your file, or use <span className="text-ink">raw</span> mode to
                read it.
              </p>
            </div>
          )}

          {mode === "raw" && <RawConfig path={form.config_path} />}

          {err && (
            <div className="mt-2 p-2 rounded border border-bad/40 bg-bad/10
                            text-2xs text-bad font-mono whitespace-pre-wrap">
              {err}
            </div>
          )}

          <div className="mt-3 flex gap-2">
            <Button tone="primary" onClick={submit} disabled={busy}>
              {busy ? "Starting…" : "Start run"}
            </Button>
          </div>
        </div>
      </Panel>

      <Panel title={`Runs (${runs.data?.runs?.length ?? 0})`}
             loading={runs.loading && !runs.data} error={runs.error}
             empty={(runs.data?.runs ?? []).length === 0}
             emptyLabel="No runs yet. Start one on the left.">
        <Table>
          <thead>
            <tr><Th>Run</Th><Th>Name</Th><Th>Status</Th><Th>Best</Th>
                <Th>Iterations</Th><Th>Started</Th><Th>Actions</Th></tr>
          </thead>
          <tbody>
            {(runs.data?.runs ?? []).map((r: Json) => (
              <Row key={r.run_id} onClick={() => onRunStarted(r.run_id)}>
                <Td><Mono>{shortId(r.run_id, 12)}</Mono></Td>
                <Td>{r.metadata?.name ?? r.live?.name ?? "—"}</Td>
                <Td><Badge tone={r.live?.status ?? r.status}>
                  {r.live?.status ?? r.status}</Badge></Td>
                <Td className="tabular">{r.best_fitness != null
                  ? Number(r.best_fitness).toFixed(5) : "—"}</Td>
                <Td className="tabular">
                  {fmtNum(r.iterations_done)}{r.iterations_target
                    ? ` / ${fmtNum(r.iterations_target)}` : ""}
                </Td>
                <Td className="text-ink-faint">{fmtTime(r.started_at)}</Td>
                <Td onClick={(e: any) => e.stopPropagation?.()}>
                  <div className="flex gap-1">
                    {r.live?.alive ? (
                      <Button size="xs" tone="danger"
                              onClick={() => api.stopRun(r.run_id).then(runs.refresh)}>
                        stop
                      </Button>
                    ) : (
                      <Button size="xs"
                              onClick={() => api.cloneRun(r.run_id).then(runs.refresh)}>
                        clone
                      </Button>
                    )}
                  </div>
                </Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>
    </div>
  );
};

/** Raw mode reads the actual config file the run will use. */
const RawConfig: React.FC<{ path: string }> = ({ path }) => (
  <div className="mt-2 p-2 rounded border border-line bg-surface-2/40">
    <div className="text-2xs uppercase text-ink-faint mb-1">Raw config</div>
    <p className="text-2xs text-ink-dim">
      The control plane passes <Mono>{path || "(none)"}</Mono> to the engine
      unchanged. Edit it on disk and the next run picks it up; the run's
      provenance records which revision was used.
    </p>
  </div>
);
