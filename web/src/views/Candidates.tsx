/**
 * Candidate Explorer (section 15).
 *
 * Server-side filtering, sorting and pagination — the client never pulls the
 * whole population to filter it locally, which is what keeps this usable at
 * tens of thousands of candidates.
 */

import React, { useState } from "react";
import { ViewProps } from "../App";
import { api, Json } from "../lib/api";
import { useAsync, useDebounced } from "../lib/hooks";
import {
  Badge, Button, Empty, Mono, Panel, Table, Td, Th, Row,
  fmtNum, fmtScore, fmtTime, scoreColor, shortId,
} from "../components/ui";

export const Candidates: React.FC<ViewProps> = ({
  runId, selectedCandidate, onSelectCandidate, liveTick,
}) => {
  const [order, setOrder] = useState<"score" | "generation" | "iteration" | "recent">("score");
  const [island, setIsland] = useState<string>("");
  const [minScore, setMinScore] = useState<string>("");
  const [generation, setGeneration] = useState<string>("");
  const [page, setPage] = useState(0);
  const limit = 100;

  const dMin = useDebounced(minScore, 300);
  const dGen = useDebounced(generation, 300);

  const q = useAsync(
    () => (runId
      ? api.candidates(runId, {
          limit, offset: page * limit, order,
          island: island || undefined,
          min_score: dMin || undefined,
          generation: dGen || undefined,
        })
      : Promise.resolve(null)),
    [runId, order, island, dMin, dGen, page, liveTick],
  );

  const rows: Json[] = q.data?.candidates ?? [];
  const total = q.data?.total ?? 0;
  const maxScore = Math.max(1, ...rows.map((r) => r.combined_score ?? 0));

  if (!runId) return <Panel title="Candidates"><Empty>Select a run.</Empty></Panel>;

  const Input: React.FC<{ value: string; onChange: (v: string) => void;
    placeholder: string; width?: string }> = ({ value, onChange, placeholder, width = "w-24" }) => (
    <input value={value} placeholder={placeholder}
           onChange={(e) => { onChange(e.target.value); setPage(0); }}
           className={`${width} bg-surface-2 border border-line rounded px-1.5 py-0.5
                       text-2xs font-mono placeholder:text-ink-faint`} />
  );

  return (
    <Panel
      title={`Candidates (${fmtNum(total)})`}
      loading={q.loading && !q.data}
      error={q.error}
      empty={rows.length === 0}
      emptyLabel={total === 0
        ? "No candidates recorded for this run yet."
        : "No candidates match these filters."}
      actions={
        <>
          <Input value={island} onChange={setIsland} placeholder="island" width="w-16" />
          <Input value={generation} onChange={setGeneration} placeholder="gen" width="w-16" />
          <Input value={minScore} onChange={setMinScore} placeholder="min score" />
          <select value={order} onChange={(e) => { setOrder(e.target.value as any); setPage(0); }}
                  className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
            <option value="score">by score</option>
            <option value="generation">by generation</option>
            <option value="iteration">by iteration</option>
            <option value="recent">most recent</option>
          </select>
          {(island || minScore || generation) && (
            <Button size="xs" onClick={() => {
              setIsland(""); setMinScore(""); setGeneration(""); setPage(0);
            }}>clear</Button>
          )}
        </>
      }
      footer={
        <div className="flex items-center gap-2">
          <Button size="xs" disabled={page === 0} onClick={() => setPage(page - 1)}>prev</Button>
          <span>rows {page * limit + 1}–{Math.min((page + 1) * limit, total)} of {fmtNum(total)}</span>
          <Button size="xs" disabled={(page + 1) * limit >= total}
                  onClick={() => setPage(page + 1)}>next</Button>
        </div>
      }
    >
      <Table>
        <thead>
          <tr>
            <Th>Candidate</Th><Th>Score</Th><Th>Gen</Th><Th>Iter</Th><Th>Island</Th>
            <Th>Cell</Th><Th>Complexity</Th><Th>Diversity</Th><Th>Len</Th>
            <Th>Parent</Th><Th>Status</Th><Th>Created</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <Row key={c.candidate_id}
                 selected={c.candidate_id === selectedCandidate}
                 onClick={() => onSelectCandidate(c.candidate_id)}>
              <Td>
                <Mono className={c.is_best ? "text-ok" : "text-ink"} title={c.candidate_id}>
                  {shortId(c.candidate_id, 12)}{c.is_best ? " ★" : ""}
                </Mono>
              </Td>
              <Td className="tabular font-mono"
                  style={{ color: scoreColor(c.combined_score, 0, maxScore) }}>
                {fmtScore(c.combined_score)}
              </Td>
              <Td className="tabular">{fmtNum(c.generation)}</Td>
              <Td className="tabular">{fmtNum(c.iteration)}</Td>
              <Td className="tabular">{c.island_id ?? "—"}</Td>
              <Td><Mono className="text-ink-faint">{c.map_elites_cell ?? "—"}</Mono></Td>
              <Td className="tabular">{fmtNum(c.complexity, 1)}</Td>
              <Td className="tabular">{fmtNum(c.diversity, 2)}</Td>
              <Td className="tabular text-ink-faint">{fmtNum(c.code_length)}</Td>
              <Td><Mono className="text-ink-faint">
                {c.parent_id ? shortId(c.parent_id, 8) : "root"}</Mono></Td>
              <Td><Badge tone={c.eval_status ?? "pending"}>{c.eval_status ?? "pending"}</Badge></Td>
              <Td className="text-ink-faint">{fmtTime(c.created_at)}</Td>
            </Row>
          ))}
        </tbody>
      </Table>
    </Panel>
  );
};
