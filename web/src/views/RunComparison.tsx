/** Run comparison (section 20): config, provenance, convergence, cost. */
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
  Badge, Mono, Panel, Spark, Table, Td, Th, Row, fmtNum, fmtScore, fmtTime, shortId,
} from "../components/ui";

export const RunComparison: React.FC<ViewProps & { runs: Json[] }> = ({ runs }) => {
  const [picked, setPicked] = useState<string[]>([]);
  const cmp = useAsync(
    () => (picked.length >= 2 ? api.compare(picked) : Promise.resolve(null)),
    [picked.join(",")]);

  const toggle = (id: string) =>
    setPicked((p) => p.includes(id) ? p.filter((x) => x !== id)
                                    : p.length < 6 ? [...p, id] : p);

  return (
    <div className="h-full grid grid-cols-[280px_1fr] gap-2 min-h-0">
      <Panel title="Select runs (2–6)"
             empty={runs.length === 0} emptyLabel="No runs yet.">
        <Table>
          <thead><tr><Th></Th><Th>Run</Th><Th>Status</Th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <Row key={r.run_id} onClick={() => toggle(r.run_id)}
                   selected={picked.includes(r.run_id)}>
                <Td>{picked.includes(r.run_id) ? "✓" : ""}</Td>
                <Td><Mono>{shortId(r.run_id, 10)}</Mono></Td>
                <Td><Badge tone={r.live?.status ?? r.status}>
                  {r.live?.status ?? r.status}</Badge></Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Comparison" loading={cmp.loading} error={cmp.error}
             empty={picked.length < 2}
             emptyLabel="Pick at least two runs to compare.">
        <div className="p-2 overflow-auto">
          <table className="text-xs w-full">
            <thead>
              <tr>
                <Th>Field</Th>
                {(cmp.data?.runs ?? []).map((r: Json) => (
                  <Th key={r.run_id}>{shortId(r.run_id, 10)}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[
                ["status", (r: Json) => r.status],
                ["started", (r: Json) => fmtTime(r.started_at)],
                ["best fitness", (r: Json) => fmtScore(r.summary?.best?.combined_score)],
                ["candidates", (r: Json) => fmtNum(r.summary?.candidates)],
                ["generation", (r: Json) => fmtNum(r.summary?.generation)],
                ["model calls", (r: Json) => fmtNum(r.summary?.model_requests)],
                ["tokens", (r: Json) => fmtNum(r.summary?.tokens)],
                ["MAP-Elites cells", (r: Json) => fmtNum(r.summary?.map_elites_occupied)],
                ["islands", (r: Json) => fmtNum(r.summary?.islands?.length)],
                ["evaluations", (r: Json) => fmtNum(r.summary?.evaluations?.total)],
                ["eval failures", (r: Json) => fmtNum(r.summary?.evaluations?.failed)],
                ["seed", (r: Json) => r.provenance?.random_seed ?? "—"],
                ["upstream commit",
                  (r: Json) => shortId(r.provenance?.upstream?.upstream_commit, 10)],
                ["python", (r: Json) => r.provenance?.python ?? "—"],
              ].map(([label, get]: any) => (
                <Row key={label}>
                  <Td className="text-ink-faint">{label}</Td>
                  {(cmp.data?.runs ?? []).map((r: Json) => (
                    <Td key={r.run_id} className="font-mono tabular">{get(r)}</Td>
                  ))}
                </Row>
              ))}
              <Row>
                <Td className="text-ink-faint">convergence</Td>
                {(cmp.data?.runs ?? []).map((r: Json) => {
                  const vals = (r.convergence ?? [])
                    .map((c: Json) => c.best)
                    .filter((v: unknown): v is number => typeof v === "number");
                  return (
                    <Td key={r.run_id}>
                      {vals.length ? <Spark values={vals} width={160} height={36} />
                                   : <span className="text-ink-faint">—</span>}
                    </Td>
                  );
                })}
              </Row>
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
};
