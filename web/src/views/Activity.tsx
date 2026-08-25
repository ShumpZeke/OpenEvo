/** Activity feed: stored event history plus the live tail, filterable. */
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
  Badge, Empty, Mono, Panel, Table, Td, Th, Row, fmtMs, fmtTime, shortId,
} from "../components/ui";

export const Activity: React.FC<ViewProps & { liveEvents: Json[] }> =
  ({ runId, onSelectCandidate, liveEvents, liveTick }) => {
  const [type, setType] = useState("");
  const [sel, setSel] = useState<Json | null>(null);
  const q = useAsync(
    () => (runId ? api.events(runId, { limit: 500, type: type || undefined })
                 : Promise.resolve(null)),
    [runId, type, liveTick]);

  const rows: Json[] = q.data?.events ?? [];
  const types = useMemo(() => {
    const s = new Set<string>();
    for (const e of [...rows, ...liveEvents]) s.add(String(e.type));
    return [...s].sort();
  }, [rows, liveEvents]);

  if (!runId) return <Panel title="Activity"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-cols-[1fr_360px] gap-2 min-h-0">
      <Panel title={`Event history (${rows.length})`}
             loading={q.loading && !q.data} error={q.error}
             empty={rows.length === 0} emptyLabel="No events stored for this run."
             actions={
               <select value={type} onChange={(e) => setType(e.target.value)}
                       className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs
                                  font-mono max-w-[220px]">
                 <option value="">all event types</option>
                 {types.map((t) => <option key={t} value={t}>{t}</option>)}
               </select>
             }>
        <Table>
          <thead>
            <tr><Th>Seq</Th><Th>Time</Th><Th>Type</Th><Th>Component</Th><Th>Status</Th>
                <Th>Candidate</Th><Th>Summary</Th><Th>Duration</Th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <Row key={e.event_id} onClick={() => setSel(e)}
                   selected={sel?.event_id === e.event_id}>
                <Td className="tabular text-ink-faint">{e.seq}</Td>
                <Td className="text-ink-faint">{fmtTime(e.timestamp)}</Td>
                <Td><Mono className={
                  e.status === "failed" ? "text-bad"
                  : e.status === "warning" ? "text-warn" : "text-info"}>{e.type}</Mono></Td>
                <Td className="text-ink-faint">{e.component}</Td>
                <Td><Badge tone={e.status}>{e.status}</Badge></Td>
                <Td>{e.candidate_id ? (
                  <button className="text-info hover:underline font-mono text-xs"
                          onClick={(ev) => { ev.stopPropagation();
                            onSelectCandidate(e.candidate_id); }}>
                    {shortId(e.candidate_id, 10)}
                  </button>) : "—"}</Td>
                <Td className="text-ink-dim truncate max-w-[280px]">{e.summary}</Td>
                <Td className="tabular text-ink-faint">
                  {e.duration_ms ? fmtMs(e.duration_ms) : ""}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Event payload">
        {!sel ? <Empty>Select an event.</Empty> : (
          <pre className="p-2 text-2xs font-mono whitespace-pre-wrap text-ink-dim">
            {JSON.stringify(sel.payload ?? sel, null, 2)}
          </pre>
        )}
      </Panel>
    </div>
  );
};
