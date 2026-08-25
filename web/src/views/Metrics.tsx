/** Metrics + resource timeline. Series are downsampled server-side. */
import React from "react";
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
  Empty, KV, Panel, Spark, Stat, fmtNum, fmtScore,
} from "../components/ui";

export const Metrics: React.FC<ViewProps> = ({ runId, liveTick }) => {
  const res = useAsync(() => (runId ? api.resources(runId, { buckets: 240 })
                                    : Promise.resolve(null)), [runId, liveTick]);
  const sum = useAsync(() => (runId ? api.summary(runId) : Promise.resolve(null)),
                       [runId, liveTick]);
  const cands = useAsync(() => (runId ? api.candidates(runId,
    { limit: 1000, order: "iteration" }) : Promise.resolve(null)), [runId, liveTick]);

  if (!runId) return <Panel title="Metrics"><Empty>Select a run.</Empty></Panel>;

  const series = res.data?.series ?? {};
  const kinds = Object.keys(series);

  const scoreSeries = [...(cands.data?.candidates ?? [])]
    .filter((c: Json) => typeof c.combined_score === "number")
    .sort((a: Json, b: Json) => (a.iteration ?? 0) - (b.iteration ?? 0))
    .map((c: Json) => c.combined_score as number);

  return (
    <div className="h-full grid grid-rows-[auto_1fr] gap-2 min-h-0">
      <Panel title="Run metrics" loading={sum.loading} error={sum.error}>
        <div className="grid grid-cols-6 divide-x divide-line">
          <Stat label="Candidates" value={fmtNum(sum.data?.candidates)} />
          <Stat label="Best fitness" value={fmtScore(sum.data?.best?.combined_score)} tone="ok" />
          <Stat label="Model calls" value={fmtNum(sum.data?.model_requests)} />
          <Stat label="Tokens" value={fmtNum(sum.data?.tokens)} />
          <Stat label="Evaluations" value={fmtNum(sum.data?.evaluations?.total)} />
          <Stat label="Eval failures" value={fmtNum(sum.data?.evaluations?.failed)}
                tone={sum.data?.evaluations?.failed ? "warn" : "default"} />
        </div>
      </Panel>

      <div className="grid grid-cols-2 gap-2 min-h-0 overflow-auto">
        <Panel title="Fitness over iterations" bodyClassName="p-3"
               empty={scoreSeries.length === 0}
               emptyLabel="No scored candidates yet."
               footer={`${scoreSeries.length} scored candidates in iteration order`}>
          <Spark values={scoreSeries} width={520} height={160} color="#4ade80" />
        </Panel>

        {kinds.length === 0 ? (
          <Panel title="Resources"
                 loading={res.loading} error={res.error}>
            <Empty>
              No resource samples recorded. The engine samples CPU/RAM/disk while
              a run is active; a finished short run may have few or none.
            </Empty>
          </Panel>
        ) : (
          kinds.map((k) => {
            const pts: Json[] = series[k] ?? [];
            const vals = pts.map((p) => p.avg as number);
            const last = vals[vals.length - 1];
            const unit = k === "ram" ? "MiB" : k === "cpu" ? "%" : "";
            return (
              <Panel key={k} title={`Resource — ${k}`} bodyClassName="p-3"
                     empty={vals.length === 0}
                     footer={`${pts.length} buckets · downsampled server-side`}>
                <div className="flex flex-col gap-2">
                  <Spark values={vals} width={520} height={120} color="#60a5fa" />
                  <div className="grid grid-cols-3 gap-2">
                    <KV k="latest" v={`${last?.toFixed(1) ?? "—"}${unit}`} />
                    <KV k="min" v={`${Math.min(...vals).toFixed(1)}${unit}`} />
                    <KV k="max" v={`${Math.max(...vals).toFixed(1)}${unit}`} />
                  </div>
                </div>
              </Panel>
            );
          })
        )}
      </div>
    </div>
  );
};
