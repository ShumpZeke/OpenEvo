/**
 * Overview / Command Center (section 37).
 *
 * The landing surface: run vitals, real run controls, convergence, island
 * rollup, provider mix and recent significant events — every value fetched
 * from the control plane.
 */

import React from "react";
import {
  ViewProps,
} from "../App";
import {
  api,
} from "../lib/api";
import {
  useAsync,
} from "../lib/hooks";
import {
  Badge, Button, Empty, KV, Mono, Panel, Spark, Stat, Table, Td, Th, Row, UnsupportedButton, fmtMs, fmtNum, fmtScore, fmtTime, scoreColor, shortId,
} from "../components/ui";

export const Overview: React.FC<ViewProps> = ({
  runId, onSelectCandidate, onNavigate, liveTick,
}) => {
  const summary = useAsync(
    () => (runId ? api.summary(runId) : Promise.resolve(null)), [runId, liveTick]);
  const run = useAsync(
    () => (runId ? api.run(runId) : Promise.resolve(null)), [runId, liveTick]);
  const caps = useAsync(() => api.capabilities(), []);
  const top = useAsync(
    () => (runId ? api.candidates(runId, { limit: 12, order: "score" })
                 : Promise.resolve(null)), [runId, liveTick]);
  const conv = useAsync(
    () => (runId ? api.candidates(runId, { limit: 500, order: "iteration" })
                 : Promise.resolve(null)), [runId, liveTick]);

  if (!runId) {
    return (
      <Panel title="Overview">
        <Empty>
          No run selected. Create one in <button className="text-info underline"
            onClick={() => onNavigate("experiments")}>Experiments</button>.
        </Empty>
      </Panel>
    );
  }

  const s = summary.data;
  const live = run.data?.live;
  const alive = live?.alive === true;
  const capability = (k: string) => caps.data?.[k] ?? { supported: false, reason: "unknown" };

  // Best-so-far curve, computed from real per-iteration candidate scores.
  const curve: number[] = [];
  let best = -Infinity;
  for (const c of [...(conv.data?.candidates ?? [])].sort(
    (a, b) => (a.iteration ?? 0) - (b.iteration ?? 0))) {
    if (typeof c.combined_score === "number") best = Math.max(best, c.combined_score);
    if (Number.isFinite(best)) curve.push(best);
  }

  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); } catch (e: any) { alert(e?.message ?? String(e)); }
    run.refresh(); summary.refresh();
  };

  return (
    <div className="h-full grid grid-rows-[auto_1fr] gap-2 min-h-0">
      {/* vitals + controls */}
      <div className="grid grid-cols-[1fr_auto] gap-2">
        <Panel title="Run vitals" loading={summary.loading} error={summary.error}>
          <div className="grid grid-cols-4 xl:grid-cols-8 divide-x divide-line">
            <Stat label="Status" value={<Badge tone={live?.status ?? run.data?.status}>
              {live?.status ?? run.data?.status ?? "—"}</Badge>} />
            <Stat label="Generation" value={fmtNum(s?.generation)}
                  sub={`iter ${fmtNum(s?.iteration)}`} />
            <Stat label="Best fitness" value={fmtScore(s?.best?.combined_score)}
                  tone="ok" sub={s?.best ? shortId(s.best.candidate_id, 10) : undefined} />
            <Stat label="Candidates" value={fmtNum(s?.candidates)} />
            <Stat label="Model calls" value={fmtNum(s?.model_requests)} />
            <Stat label="Tokens" value={fmtNum(s?.tokens)} />
            <Stat label="MAP-Elites" value={fmtNum(s?.map_elites_occupied)}
                  sub="cells occupied" />
            <Stat label="Evaluations"
                  value={fmtNum(s?.evaluations?.total)}
                  tone={s?.evaluations?.failed ? "warn" : "default"}
                  sub={`${fmtNum(s?.evaluations?.failed ?? 0)} failed`} />
          </div>
        </Panel>

        <Panel title="Controls">
          <div className="p-2 flex flex-col gap-1.5 w-56">
            <Button tone="danger" disabled={!alive}
                    onClick={() => act(() => api.stopRun(runId, false))}
                    title={capability("graceful_stop").reason}>
              Graceful stop
            </Button>
            <Button tone="danger" disabled={!alive}
                    onClick={() => act(() => api.stopRun(runId, true))}
                    title={capability("force_stop").reason}>
              Force stop
            </Button>
            <Button disabled={!alive}
                    onClick={() => act(() => api.checkpointNow(runId))}
                    title={capability("checkpoint_now").reason}>
              Checkpoint now
            </Button>
            <Button disabled={alive}
                    onClick={() => act(() => api.resumeRun(runId))}
                    title={capability("resume_checkpoint").reason}>
              Resume from checkpoint
            </Button>
            <Button onClick={() => act(() => api.cloneRun(runId))}
                    title={capability("clone_experiment").reason}>
              Clone experiment
            </Button>
            {/* Controls upstream cannot support are disabled with the reason. */}
            <UnsupportedButton reason={capability("pause_resume_in_place").reason}>
              Pause / resume
            </UnsupportedButton>
            <UnsupportedButton reason={capability("fork_from_candidate").reason}>
              Fork from candidate
            </UnsupportedButton>
          </div>
        </Panel>
      </div>

      {/* lower grid */}
      <div className="grid grid-cols-3 gap-2 min-h-0">
        <Panel title="Convergence" bodyClassName="p-3"
               empty={curve.length === 0}
               emptyLabel="No scored candidates yet."
               footer={`best-so-far across ${fmtNum(curve.length)} scored candidates`}>
          <div className="flex flex-col gap-2">
            <Spark values={curve} width={420} height={120} color="#4ade80" />
            <div className="grid grid-cols-2 gap-x-4">
              <KV k="first" v={fmtScore(curve[0])} />
              <KV k="best" v={fmtScore(curve[curve.length - 1])} />
              <KV k="improvement" v={
                curve.length > 1 ? fmtScore(curve[curve.length - 1] - curve[0]) : "—"} />
              <KV k="plateau" v={(() => {
                let n = 0;
                for (let i = curve.length - 1; i > 0 && curve[i] === curve[i - 1]; i--) n++;
                return `${n} candidates`;
              })()} />
            </div>
          </div>
        </Panel>

        <Panel title="Islands"
               actions={<Button size="xs" onClick={() => onNavigate("islands")}>Open</Button>}
               empty={(s?.islands ?? []).length === 0}
               emptyLabel="No island data yet.">
          <Table>
            <thead>
              <tr><Th>Island</Th><Th>Pop</Th><Th>Best</Th><Th>Median</Th>
                  <Th>Diversity</Th><Th>Migrants</Th></tr>
            </thead>
            <tbody>
              {(s?.islands ?? []).map((i: any) => (
                <Row key={i.island_id}>
                  <Td><Mono>{i.island_id}</Mono></Td>
                  <Td className="tabular">{fmtNum(i.population)}</Td>
                  <Td className="tabular" style={{ color: scoreColor(i.best_score, 0,
                        Math.max(1, s?.best?.combined_score ?? 1)) }}>
                    {fmtScore(i.best_score)}
                  </Td>
                  <Td className="tabular">{fmtScore(i.median_score)}</Td>
                  <Td className="tabular">{fmtNum(i.diversity, 2)}</Td>
                  <Td className="tabular text-ink-faint">
                    ↑{fmtNum(i.migrants_sent)} ↓{fmtNum(i.migrants_received)}
                  </Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>

        <Panel title="Providers used"
               actions={<Button size="xs" onClick={() => onNavigate("models")}>Open</Button>}
               empty={(s?.providers ?? []).length === 0}
               emptyLabel="No model requests recorded yet.">
          <Table>
            <thead>
              <tr><Th>Provider</Th><Th>Model</Th><Th>Reqs</Th><Th>Tokens</Th>
                  <Th>Latency</Th><Th>429</Th></tr>
            </thead>
            <tbody>
              {(s?.providers ?? []).map((p: any, i: number) => (
                <Row key={i}>
                  <Td><Mono>{p.provider}</Mono></Td>
                  <Td><Mono className="text-ink-dim">{p.model}</Mono></Td>
                  <Td className="tabular">{fmtNum(p.requests)}</Td>
                  <Td className="tabular">{fmtNum(p.tokens)}</Td>
                  <Td className="tabular">{fmtMs(p.avg_latency)}</Td>
                  <Td className="tabular">
                    {p.rate_limited ? <span className="text-warn">{p.rate_limited}</span> : "0"}
                  </Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>

        <Panel className="col-span-2" title="Top candidates"
               actions={<Button size="xs" onClick={() => onNavigate("candidates")}>
                 All candidates</Button>}
               loading={top.loading} error={top.error}
               empty={(top.data?.candidates ?? []).length === 0}
               emptyLabel="No candidates recorded yet.">
          <Table>
            <thead>
              <tr><Th>Candidate</Th><Th>Score</Th><Th>Gen</Th><Th>Island</Th>
                  <Th>Cell</Th><Th>Parent</Th><Th>Status</Th></tr>
            </thead>
            <tbody>
              {(top.data?.candidates ?? []).map((c: any) => (
                <Row key={c.candidate_id} onClick={() => onSelectCandidate(c.candidate_id)}>
                  <Td>
                    <Mono className={c.is_best ? "text-ok" : "text-ink"}>
                      {shortId(c.candidate_id, 12)}
                    </Mono>
                    {c.is_best ? " ★" : ""}
                  </Td>
                  <Td className="tabular">{fmtScore(c.combined_score)}</Td>
                  <Td className="tabular">{fmtNum(c.generation)}</Td>
                  <Td className="tabular">{c.island_id ?? "—"}</Td>
                  <Td><Mono className="text-ink-faint">{c.map_elites_cell ?? "—"}</Mono></Td>
                  <Td><Mono className="text-ink-faint">
                    {c.parent_id ? shortId(c.parent_id, 8) : "root"}</Mono></Td>
                  <Td><Badge tone={c.eval_status ?? "pending"}>
                    {c.eval_status ?? "pending"}</Badge></Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>

        <Panel title="Run provenance"
               footer="Everything needed to reproduce this run">
          <div className="p-2">
            <KV k="run" v={shortId(runId, 16)} />
            <KV k="experiment" v={shortId(run.data?.experiment_id, 16)} />
            <KV k="started" v={fmtTime(run.data?.started_at)} />
            <KV k="pid" v={live?.pid ?? "—"} />
            <KV k="output" v={<span className="break-all">{live?.output_dir ?? "—"}</span>} />
            <KV k="checkpoints" v={fmtNum(run.data?.counts?.checkpoints)} />
            <KV k="events stored" v={fmtNum(run.data?.counts?.events)} />
            {Object.entries(run.data?.provenance ?? {})
              .filter(([k]) => !["upstream", "models"].includes(k))
              .map(([k, v]) => (
                <KV key={k} k={k} v={typeof v === "object"
                  ? JSON.stringify(v).slice(0, 60) : String(v)} />
              ))}
            {run.data?.provenance?.upstream && (
              <KV k="upstream commit"
                  v={shortId(run.data.provenance.upstream.upstream_commit, 12)} />
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
};
