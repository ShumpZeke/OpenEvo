/**
 * Island Lab (section 13).
 *
 * Per-island population, fitness spread, diversity, stagnation and migration
 * flow. The migration matrix is built from recorded migration events, so an
 * arrow exists only where a candidate actually moved.
 */

import React, {
  useMemo,
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
  Empty, Mono, Panel, Table, Td, Th, Row, fmtNum, fmtScore, fmtTime, scoreColor, shortId,
} from "../components/ui";

export const IslandsLab: React.FC<ViewProps> = ({ runId, onSelectCandidate, liveTick }) => {
  const q = useAsync(() => (runId ? api.islands(runId) : Promise.resolve(null)),
                     [runId, liveTick]);
  const islands: Json[] = q.data?.islands ?? [];
  const migrations: Json[] = q.data?.migrations ?? [];

  const matrix = useMemo(() => {
    const m = new Map<string, number>();
    for (const mig of migrations) {
      if (mig.source_island === null || mig.target_island === null) continue;
      const k = `${mig.source_island}->${mig.target_island}`;
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return m;
  }, [migrations]);

  const maxBest = Math.max(1, ...islands.map((i) => i.best_score ?? 0));
  const ids = islands.map((i) => i.island_id);

  if (!runId) return <Panel title="Islands"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-rows-[auto_1fr] gap-2 min-h-0">
      <Panel title="Islands" loading={q.loading && !q.data} error={q.error}
             empty={islands.length === 0}
             emptyLabel="No island data recorded yet.">
        <Table>
          <thead>
            <tr>
              <Th>Island</Th><Th>Population</Th><Th>Best</Th><Th>Median</Th>
              <Th>Diversity</Th><Th>Generation</Th><Th>Stagnation</Th>
              <Th>Model calls</Th><Th>Tokens</Th><Th>Eval fails</Th>
              <Th>Sent</Th><Th>Received</Th><Th>Best candidate</Th><Th>Updated</Th>
            </tr>
          </thead>
          <tbody>
            {islands.map((i) => (
              <Row key={i.island_id}>
                <Td><Mono className="text-ink">{i.island_id}</Mono></Td>
                <Td className="tabular">{fmtNum(i.population)}</Td>
                <Td className="tabular" style={{ color: scoreColor(i.best_score, 0, maxBest) }}>
                  {fmtScore(i.best_score)}
                </Td>
                <Td className="tabular">{fmtScore(i.median_score)}</Td>
                <Td className="tabular">{fmtNum(i.diversity, 2)}</Td>
                <Td className="tabular">{fmtNum(i.generation)}</Td>
                <Td className="tabular">
                  {i.stagnation_generations != null
                    ? <span className={i.stagnation_generations > 10 ? "text-warn" : ""}>
                        {i.stagnation_generations}
                      </span>
                    : "—"}
                </Td>
                <Td className="tabular">{fmtNum(i.model_calls)}</Td>
                <Td className="tabular">{fmtNum(i.tokens)}</Td>
                <Td className="tabular">
                  {i.eval_failures ? <span className="text-bad">{i.eval_failures}</span> : "0"}
                </Td>
                <Td className="tabular text-ink-faint">{fmtNum(i.migrants_sent)}</Td>
                <Td className="tabular text-ink-faint">{fmtNum(i.migrants_received)}</Td>
                <Td>
                  {i.best_candidate_id ? (
                    <button className="text-info hover:underline font-mono text-xs"
                            onClick={() => onSelectCandidate(i.best_candidate_id)}>
                      {shortId(i.best_candidate_id, 10)}
                    </button>
                  ) : "—"}
                </Td>
                <Td className="text-ink-faint">{fmtTime(i.updated_at)}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <div className="grid grid-cols-2 gap-2 min-h-0">
        <Panel title="Migration matrix"
               empty={matrix.size === 0}
               emptyLabel="No migrations recorded yet. Migration runs on an interval, so an early run may legitimately show none."
               footer="rows = source island, columns = target island, value = migrants moved">
          <div className="p-3 overflow-auto">
            <table className="text-xs border-collapse">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-ink-faint text-2xs">from ╲ to</th>
                  {ids.map((t) => (
                    <th key={t} className="px-2 py-1 text-ink-faint text-2xs font-mono">{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ids.map((s) => (
                  <tr key={s}>
                    <td className="px-2 py-1 text-ink-faint text-2xs font-mono">{s}</td>
                    {ids.map((t) => {
                      const n = matrix.get(`${s}->${t}`) ?? 0;
                      return (
                        <td key={t} className="px-2 py-1 text-center font-mono tabular">
                          {s === t ? (
                            <span className="text-ink-faint/40">·</span>
                          ) : n ? (
                            <span className="px-1.5 py-0.5 rounded bg-info/20 text-info">{n}</span>
                          ) : (
                            <span className="text-ink-faint/40">0</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title={`Recent migrations (${migrations.length})`}
               empty={migrations.length === 0}
               emptyLabel="No migration events recorded.">
          <Table>
            <thead>
              <tr><Th>Time</Th><Th>Gen</Th><Th>From</Th><Th>To</Th><Th>Candidate</Th></tr>
            </thead>
            <tbody>
              {migrations.slice(0, 200).map((m) => (
                <Row key={m.migration_id}>
                  <Td className="text-ink-faint">{fmtTime(m.timestamp)}</Td>
                  <Td className="tabular">{fmtNum(m.generation)}</Td>
                  <Td><Mono>{m.source_island ?? "—"}</Mono></Td>
                  <Td><Mono className="text-info">{m.target_island ?? "—"}</Mono></Td>
                  <Td>
                    <button className="text-info hover:underline font-mono text-xs"
                            onClick={() => m.new_candidate_id
                              && onSelectCandidate(m.new_candidate_id)}>
                      {shortId(m.new_candidate_id ?? m.candidate_id, 12)}
                    </button>
                  </Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>
      </div>
    </div>
  );
};
