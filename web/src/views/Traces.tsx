/**
 * Traces: events grouped by trace/span so one candidate's full path —
 * sample → mutate → evaluate → place — reads as a single sequence.
 */
import React, {
  useMemo, useState,
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
  Empty, Mono, Panel, Table, Td, Th, Row, cx, fmtMs, fmtTime, shortId,
} from "../components/ui";

export const Traces: React.FC<ViewProps> = ({ runId, onSelectCandidate, liveTick }) => {
  const [sel, setSel] = useState<string | null>(null);
  const q = useAsync(() => (runId ? api.events(runId, { limit: 1000 })
                                  : Promise.resolve(null)), [runId, liveTick]);

  const byCandidate = useMemo(() => {
    const m = new Map<string, Json[]>();
    for (const e of q.data?.events ?? []) {
      if (!e.candidate_id) continue;
      (m.get(e.candidate_id) ?? m.set(e.candidate_id, []).get(e.candidate_id)!).push(e);
    }
    for (const list of m.values()) list.sort((a, b) => a.timestamp - b.timestamp);
    return m;
  }, [q.data]);

  const spans = sel ? byCandidate.get(sel) ?? [] : [];
  const t0 = spans.length ? spans[0].timestamp : 0;
  const t1 = spans.length ? Math.max(...spans.map((s) => s.timestamp)) : 1;
  const width = Math.max(0.001, t1 - t0);

  if (!runId) return <Panel title="Traces"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-cols-[300px_1fr] gap-2 min-h-0">
      <Panel title={`Candidate traces (${byCandidate.size})`}
             loading={q.loading && !q.data} error={q.error}
             empty={byCandidate.size === 0}
             emptyLabel="No candidate-scoped events yet.">
        <Table>
          <thead><tr><Th>Candidate</Th><Th>Spans</Th></tr></thead>
          <tbody>
            {[...byCandidate.entries()].map(([cid, list]) => (
              <Row key={cid} onClick={() => setSel(cid)} selected={sel === cid}>
                <Td><Mono>{shortId(cid, 14)}</Mono></Td>
                <Td className="tabular">{list.length}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title={sel ? `Trace — ${shortId(sel, 18)}` : "Trace"}
             empty={spans.length === 0}
             emptyLabel="Select a candidate to see its trace."
             actions={sel && (
               <button className="text-info hover:underline text-2xs"
                       onClick={() => onSelectCandidate(sel)}>open in inspector</button>
             )}
             footer={spans.length ? `${((t1 - t0) * 1000).toFixed(0)}ms span` : undefined}>
        <div className="p-2 space-y-0.5">
          {spans.map((s) => {
            const left = ((s.timestamp - t0) / width) * 100;
            const w = s.duration_ms
              ? Math.max(0.6, (s.duration_ms / 1000 / width) * 100) : 0.6;
            return (
              <div key={s.event_id} className="flex items-center gap-2 group">
                <Mono className="w-56 shrink-0 truncate text-2xs text-ink-dim">{s.type}</Mono>
                <div className="flex-1 h-4 relative bg-surface-2/40 rounded">
                  <div className={cx("absolute h-full rounded",
                        s.status === "failed" ? "bg-bad/70"
                        : s.status === "warning" ? "bg-warn/70" : "bg-info/60")}
                       style={{ left: `${Math.min(99, left)}%`,
                                width: `${Math.min(100 - left, w)}%` }}
                       title={`${s.type} @ ${fmtTime(s.timestamp)}`} />
                </div>
                <span className="w-16 text-right text-2xs text-ink-faint tabular shrink-0">
                  {s.duration_ms ? fmtMs(s.duration_ms) : ""}
                </span>
              </div>
            );
          })}
        </div>
      </Panel>
    </div>
  );
};
