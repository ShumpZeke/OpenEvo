/** Evaluator observability (section 18): stages, exit codes, metrics, output. */
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
  Badge, Button, Empty, KV, Panel, Table, Td, Th, Row, fmtMs, fmtNum, fmtScore, fmtTime, shortId,
} from "../components/ui";

export const Evaluators: React.FC<ViewProps> = ({ runId, onSelectCandidate, liveTick }) => {
  const [status, setStatus] = useState<string>("");
  const [sel, setSel] = useState<Json | null>(null);
  const q = useAsync(
    () => (runId ? api.evaluations(runId, { limit: 300, status: status || undefined })
                 : Promise.resolve(null)),
    [runId, status, liveTick]);
  const rows: Json[] = q.data?.evaluations ?? [];
  const failed = rows.filter((r) => r.status === "failed").length;

  if (!runId) return <Panel title="Evaluators"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-cols-[1fr_340px] gap-2 min-h-0">
      <Panel title={`Evaluations (${fmtNum(q.data?.total ?? 0)})`}
             loading={q.loading && !q.data} error={q.error}
             empty={rows.length === 0} emptyLabel="No evaluations recorded."
             actions={
               <>
                 {["", "ok", "failed", "running"].map((s) => (
                   <Button key={s || "all"} size="xs" onClick={() => setStatus(s)}>
                     {s || "all"}
                   </Button>
                 ))}
               </>
             }
             footer={`${failed} failed of ${rows.length} shown`}>
        <Table>
          <thead>
            <tr><Th>Started</Th><Th>Candidate</Th><Th>Status</Th><Th>Stage</Th>
                <Th>Duration</Th><Th>Score</Th><Th>Exit</Th><Th>Failure</Th></tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <Row key={e.evaluation_id} onClick={() => setSel(e)}
                   selected={sel?.evaluation_id === e.evaluation_id}>
                <Td className="text-ink-faint">{fmtTime(e.started_at)}</Td>
                <Td>
                  <button className="text-info hover:underline font-mono text-xs"
                          onClick={(ev) => { ev.stopPropagation();
                            e.candidate_id && onSelectCandidate(e.candidate_id); }}>
                    {shortId(e.candidate_id, 12)}
                  </button>
                </Td>
                <Td><Badge tone={e.status}>{e.status}</Badge></Td>
                <Td className="tabular">{e.stage ?? 0}</Td>
                <Td className="tabular">{fmtMs(e.duration_ms)}</Td>
                <Td className="tabular">{fmtScore(e.combined_score)}</Td>
                <Td className="tabular">{e.exit_code ?? "—"}</Td>
                <Td className="text-bad text-2xs">{e.failure_class ?? ""}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Evaluation detail">
        {!sel ? <Empty>Select an evaluation.</Empty> : (
          <div className="p-2">
            <KV k="evaluation" v={shortId(sel.evaluation_id, 16)} />
            <KV k="candidate" v={shortId(sel.candidate_id, 16)} />
            <KV k="evaluator" v={sel.evaluator_id ?? "—"} />
            <KV k="status" v={<Badge tone={sel.status}>{sel.status}</Badge>} />
            <KV k="stage" v={sel.stage ?? 0} />
            <KV k="duration" v={fmtMs(sel.duration_ms)} />
            <KV k="exit code" v={sel.exit_code ?? "—"} />
            <KV k="timed out" v={sel.timed_out ? "yes" : "no"} />
            <KV k="score" v={fmtScore(sel.combined_score)} />
            {Object.keys(sel.raw_metrics ?? {}).length > 0 && (
              <div className="mt-2">
                <div className="text-2xs uppercase text-ink-faint mb-0.5">Raw metrics</div>
                {Object.entries(sel.raw_metrics).map(([k, v]) => (
                  <KV key={k} k={k} v={typeof v === "number" ? fmtScore(v) : String(v)} />
                ))}
              </div>
            )}
            {sel.stderr_excerpt && (
              <details open className="mt-2">
                <summary className="text-2xs text-ink-faint cursor-pointer">stderr</summary>
                <pre className="text-2xs font-mono text-bad whitespace-pre-wrap mt-1">
                  {sel.stderr_excerpt}</pre>
              </details>
            )}
            {sel.stdout_excerpt && (
              <details className="mt-2">
                <summary className="text-2xs text-ink-faint cursor-pointer">stdout</summary>
                <pre className="text-2xs font-mono text-ink-dim whitespace-pre-wrap mt-1">
                  {sel.stdout_excerpt}</pre>
              </details>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
};
