/** Errors view: every failed/warning event, grouped by cause. */
import React, { useMemo, useState } from "react";
import { ViewProps } from "../App";
import { api, Json } from "../lib/api";
import { useAsync } from "../lib/hooks";
import {
  Badge, Empty, KV, Mono, Panel, Table, Td, Th, Row, fmtTime, shortId,
} from "../components/ui";

export const Errors: React.FC<ViewProps> = ({ runId, onSelectCandidate, liveTick }) => {
  const [sel, setSel] = useState<Json | null>(null);
  const failed = useAsync(
    () => (runId ? api.events(runId, { limit: 300, status: "failed" })
                 : Promise.resolve(null)), [runId, liveTick]);
  const warned = useAsync(
    () => (runId ? api.events(runId, { limit: 200, status: "warning" })
                 : Promise.resolve(null)), [runId, liveTick]);

  const rows = useMemo(
    () => [...(failed.data?.events ?? []), ...(warned.data?.events ?? [])]
      .sort((a, b) => b.timestamp - a.timestamp),
    [failed.data, warned.data]);

  const grouped = useMemo(() => {
    const g = new Map<string, number>();
    for (const e of rows) {
      const key = e.payload?.error?.type ?? e.type;
      g.set(key, (g.get(key) ?? 0) + 1);
    }
    return [...g.entries()].sort((a, b) => b[1] - a[1]);
  }, [rows]);

  if (!runId) return <Panel title="Errors"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-cols-[220px_1fr_340px] gap-2 min-h-0">
      <Panel title="By cause" empty={grouped.length === 0}
             emptyLabel="No errors recorded.">
        <Table>
          <thead><tr><Th>Cause</Th><Th>Count</Th></tr></thead>
          <tbody>
            {grouped.map(([k, n]) => (
              <Row key={k}>
                <Td><Mono className="text-bad text-2xs">{k}</Mono></Td>
                <Td className="tabular">{n}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title={`Errors & warnings (${rows.length})`}
             loading={failed.loading && !failed.data} error={failed.error}
             empty={rows.length === 0}
             emptyLabel="No errors or warnings for this run. ">
        <Table>
          <thead>
            <tr><Th>Time</Th><Th>Status</Th><Th>Type</Th><Th>Component</Th>
                <Th>Candidate</Th><Th>Summary</Th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <Row key={e.event_id} onClick={() => setSel(e)}
                   selected={sel?.event_id === e.event_id}>
                <Td className="text-ink-faint">{fmtTime(e.timestamp)}</Td>
                <Td><Badge tone={e.status}>{e.status}</Badge></Td>
                <Td><Mono className="text-2xs">{e.type}</Mono></Td>
                <Td className="text-ink-faint">{e.component}</Td>
                <Td>{e.candidate_id ? (
                  <button className="text-info hover:underline font-mono text-xs"
                          onClick={(ev) => { ev.stopPropagation();
                            onSelectCandidate(e.candidate_id); }}>
                    {shortId(e.candidate_id, 10)}</button>) : "—"}</Td>
                <Td className="text-ink-dim truncate max-w-[360px]">{e.summary}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Detail">
        {!sel ? <Empty>Select an entry.</Empty> : (
          <div className="p-2">
            <KV k="type" v={sel.type} />
            <KV k="status" v={sel.status} />
            <KV k="component" v={sel.component} />
            <KV k="time" v={fmtTime(sel.timestamp)} />
            {sel.payload?.error && (
              <>
                <div className="text-2xs uppercase text-ink-faint mt-2 mb-0.5">Error</div>
                <pre className="text-2xs font-mono text-bad whitespace-pre-wrap">
                  {JSON.stringify(sel.payload.error, null, 2)}</pre>
              </>
            )}
            <details className="mt-2">
              <summary className="text-2xs text-ink-faint cursor-pointer">full payload</summary>
              <pre className="text-2xs font-mono text-ink-dim whitespace-pre-wrap mt-1">
                {JSON.stringify(sel.payload ?? sel, null, 2)}</pre>
            </details>
          </div>
        )}
      </Panel>
    </div>
  );
};
