/** Checkpoint Center (section 20 / task 28): list, resume, delete — all real. */
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
  Button, Empty, Mono, Panel, Table, Td, Th, Row, fmtBytes, fmtNum, fmtScore, fmtTime,
} from "../components/ui";

export const Checkpoints: React.FC<ViewProps> = ({ runId, liveTick }) => {
  const [busy, setBusy] = useState(false);
  const q = useAsync(() => (runId ? api.checkpoints(runId) : Promise.resolve(null)),
                     [runId, liveTick]);
  const run = useAsync(() => (runId ? api.run(runId) : Promise.resolve(null)),
                       [runId, liveTick]);
  const rows: Json[] = q.data?.checkpoints ?? [];
  const alive = run.data?.live?.alive === true;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); } catch (e: any) { alert(e?.message ?? String(e)); }
    finally { setBusy(false); q.refresh(); }
  };

  if (!runId) return <Panel title="Checkpoints"><Empty>Select a run.</Empty></Panel>;

  return (
    <Panel title={`Checkpoints (${rows.length})`}
           loading={q.loading && !q.data} error={q.error}
           empty={rows.length === 0}
           emptyLabel="No checkpoints written yet. They are created on the configured interval, or on demand."
           actions={
             <Button size="xs" disabled={!alive || busy}
                     title={alive ? "Request a checkpoint at the next iteration boundary"
                                  : "Run is not active"}
                     onClick={() => act(() => api.checkpointNow(runId))}>
               checkpoint now
             </Button>
           }
           footer="Listing reads the filesystem, so a checkpoint deleted outside the UI disappears here too.">
      <Table>
        <thead>
          <tr><Th>Iteration</Th><Th>Created</Th><Th>Size</Th><Th>Programs</Th>
              <Th>Best score</Th><Th>Path</Th><Th>Actions</Th></tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <Row key={c.checkpoint_id}>
              <Td><Mono className="text-ink">{c.iteration}</Mono></Td>
              <Td className="text-ink-faint">{fmtTime(c.created_at)}</Td>
              <Td className="tabular">{fmtBytes(c.size_bytes)}</Td>
              <Td className="tabular">{fmtNum(c.num_programs)}</Td>
              <Td className="tabular">{fmtScore(c.best_score)}</Td>
              <Td><Mono className="text-ink-faint text-2xs" title={c.path}>
                …{String(c.path).slice(-46)}</Mono></Td>
              <Td>
                <div className="flex gap-1">
                  <Button size="xs" disabled={busy || alive}
                          title={alive ? "Stop the run before resuming from a checkpoint"
                                       : "Start a new run seeded from this checkpoint"}
                          onClick={() => act(() =>
                            api.resumeRun(runId, { checkpoint: c.path }))}>
                    resume
                  </Button>
                  <Button size="xs" tone="danger" disabled={busy}
                          onClick={() => {
                            if (confirm(`Delete checkpoint ${c.iteration}? This removes it from disk permanently.`)) {
                              act(() => api.deleteCheckpoint(runId, c.iteration));
                            }
                          }}>
                    delete
                  </Button>
                </div>
              </Td>
            </Row>
          ))}
        </tbody>
      </Table>
    </Panel>
  );
};
