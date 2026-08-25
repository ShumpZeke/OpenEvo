/**
 * MAP-Elites Lab (section 12).
 *
 * Not "one static heatmap": axes are selectable, the generation scrubber
 * reconstructs historical occupancy from map_elites_history, islands can be
 * viewed separately or overlaid, and clicking a cell opens its occupant.
 *
 * Island scoping is real rather than cosmetic — upstream keeps a separate
 * feature map per island, so "all islands" shows the best occupant per cell
 * across islands and names which island holds it.
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
  Button, Empty, KV, Mono, Panel, Table, Td, Th, Row, cx, fmtNum, fmtScore, fmtTime, scoreColor, shortId,
} from "../components/ui";

export const MapElitesLab: React.FC<ViewProps> = ({
  runId, onSelectCandidate, liveTick,
}) => {
  const [island, setIsland] = useState<number | "all">("all");
  const [generation, setGeneration] = useState<number | null>(null);
  const [axisX, setAxisX] = useState(0);
  const [axisY, setAxisY] = useState(1);
  const [hover, setHover] = useState<Json | null>(null);

  const islandsQ = useAsync(
    () => (runId ? api.islands(runId) : Promise.resolve(null)), [runId, liveTick]);
  const grid = useAsync(
    () => (runId
      ? api.mapElites(runId, {
          island: island === "all" ? undefined : island,
          generation: generation ?? undefined,
        })
      : Promise.resolve(null)),
    [runId, island, generation, liveTick],
  );

  const dims: string[] = grid.data?.dimensions ?? [];
  const maxGen: number | null = grid.data?.max_generation ?? null;

  const { cells, coordsX, coordsY, minScore, maxScore, occupancy } = useMemo(() => {
    const raw: Json[] = grid.data?.cells ?? [];
    // Explicitly typed: the spread of a `Json` widens to `any` keys, and without
    // the annotation TS narrows `parsed` to just `{ coordsArr }`.
    const parsed: (Json & { coordsArr: number[] })[] = raw.map((c: Json) => {
      const coords: number[] = Array.isArray(c.coords)
        ? c.coords
        : String(c.cell_key ?? "").split("-").map(Number);
      return { ...c, coordsArr: coords };
    });
    const xs = new Set<number>(), ys = new Set<number>();
    for (const c of parsed) {
      xs.add(c.coordsArr[axisX] ?? 0);
      ys.add(c.coordsArr[axisY] ?? 0);
    }
    const scores = parsed.map((c: Json) => c.score)
      .filter((s: unknown): s is number => typeof s === "number");
    // Best occupant per (x,y), so overlaying islands never silently hides one.
    const map = new Map<string, Json>();
    for (const c of parsed) {
      const key = `${c.coordsArr[axisX] ?? 0}|${c.coordsArr[axisY] ?? 0}`;
      const prev = map.get(key);
      if (!prev || (c.score ?? -Infinity) > (prev.score ?? -Infinity)) map.set(key, c);
    }
    const sortedX = [...xs].sort((a, b) => a - b);
    const sortedY = [...ys].sort((a, b) => a - b);
    return {
      cells: map,
      coordsX: sortedX,
      coordsY: sortedY,
      minScore: scores.length ? Math.min(...scores) : 0,
      maxScore: scores.length ? Math.max(...scores) : 1,
      occupancy: sortedX.length && sortedY.length
        ? map.size / (sortedX.length * sortedY.length) : 0,
    };
  }, [grid.data, axisX, axisY]);

  if (!runId) return <Panel title="MAP-Elites Lab"><Empty>Select a run.</Empty></Panel>;

  const islandList: Json[] = islandsQ.data?.islands ?? [];

  return (
    <div className="h-full grid grid-cols-[1fr_300px] gap-2 min-h-0">
      <Panel
        title="MAP-Elites grid"
        loading={grid.loading && !grid.data}
        error={grid.error}
        empty={cells.size === 0}
        emptyLabel={generation !== null
          ? `No cells were occupied at or before generation ${generation}.`
          : "No MAP-Elites cells recorded yet."}
        bodyClassName="p-3"
        actions={
          <>
            <select value={String(island)}
                    onChange={(e) => setIsland(e.target.value === "all"
                      ? "all" : Number(e.target.value))}
                    className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
              <option value="all">all islands</option>
              {islandList.map((i) => (
                <option key={i.island_id} value={i.island_id}>island {i.island_id}</option>
              ))}
            </select>
            {dims.length > 2 && (
              <>
                <select value={axisX} onChange={(e) => setAxisX(Number(e.target.value))}
                        className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
                  {dims.map((d, i) => <option key={d} value={i}>x: {d}</option>)}
                </select>
                <select value={axisY} onChange={(e) => setAxisY(Number(e.target.value))}
                        className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
                  {dims.map((d, i) => <option key={d} value={i}>y: {d}</option>)}
                </select>
              </>
            )}
          </>
        }
        footer={
          <div className="flex items-center gap-3">
            <span>{fmtNum(cells.size)} occupied · {(occupancy * 100).toFixed(1)}% of the
              {" "}{coordsX.length}×{coordsY.length} window</span>
            <span className="text-ink-faint">
              dims: {dims.length ? dims.join(" × ") : "—"}
            </span>
          </div>
        }
      >
        <div className="flex flex-col h-full gap-2">
          {/* generation scrubber */}
          {maxGen !== null && maxGen > 0 && (
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-2xs uppercase text-ink-faint w-16">Generation</span>
              <input
                type="range" min={0} max={maxGen}
                value={generation ?? maxGen}
                onChange={(e) => setGeneration(Number(e.target.value))}
                className="flex-1 accent-info"
              />
              <Mono className="w-24 text-right">
                {generation === null ? `live (${maxGen})` : generation}
              </Mono>
              {generation !== null && (
                <Button size="xs" onClick={() => setGeneration(null)}>live</Button>
              )}
            </div>
          )}

          <div className="flex-1 min-h-0 overflow-auto">
            {coordsX.length > 0 && (
              <div className="inline-grid gap-px"
                   style={{
                     gridTemplateColumns: `auto repeat(${coordsX.length}, minmax(18px, 34px))`,
                   }}>
                <div />
                {coordsX.map((x) => (
                  <div key={x} className="text-2xs text-ink-faint text-center pb-0.5">{x}</div>
                ))}
                {coordsY.map((y) => (
                  <React.Fragment key={y}>
                    <div className="text-2xs text-ink-faint pr-1 flex items-center
                                    justify-end">{y}</div>
                    {coordsX.map((x) => {
                      const c = cells.get(`${x}|${y}`);
                      return (
                        <button
                          key={`${x}-${y}`}
                          onMouseEnter={() => setHover(c ?? null)}
                          onClick={() => c?.candidate_id && onSelectCandidate(c.candidate_id)}
                          disabled={!c}
                          title={c
                            ? `cell ${x}-${y} · score ${fmtScore(c.score)} · island ${c.island_id}`
                            : `cell ${x}-${y} · empty`}
                          className={cx(
                            "aspect-square rounded-[2px] border transition-transform",
                            c ? "border-line-strong hover:scale-125 hover:z-10 cursor-pointer"
                              : "border-line/40 bg-surface-2/30 cursor-default",
                          )}
                          style={c ? {
                            background: scoreColor(c.score, minScore, maxScore),
                          } : undefined}
                        />
                      );
                    })}
                  </React.Fragment>
                ))}
              </div>
            )}
          </div>

          <div className="shrink-0 flex items-center gap-2">
            <span className="text-2xs text-ink-faint">{fmtScore(minScore)}</span>
            <div className="h-2 flex-1 rounded" style={{
              background: `linear-gradient(to right, ${scoreColor(minScore, minScore, maxScore)}, ${scoreColor(maxScore, minScore, maxScore)})`,
            }} />
            <span className="text-2xs text-ink-faint">{fmtScore(maxScore)}</span>
          </div>
        </div>
      </Panel>

      <div className="flex flex-col gap-2 min-h-0">
        <Panel title="Cell detail">
          {!hover ? (
            <Empty>Hover a cell.</Empty>
          ) : (
            <div className="p-2">
              <KV k="cell" v={hover.cell_key} />
              <KV k="island" v={hover.island_id ?? "—"} />
              <KV k="occupant" v={
                <button className="text-info hover:underline font-mono"
                        onClick={() => onSelectCandidate(hover.candidate_id)}>
                  {shortId(hover.candidate_id, 14)}
                </button>} />
              <KV k="score" v={fmtScore(hover.score)} />
              <KV k="generation" v={fmtNum(hover.generation)} />
              <KV k="replacements" v={fmtNum(hover.replacements)} />
              <KV k="updated" v={fmtTime(hover.updated_at)} />
              {Array.isArray(hover.coords) && dims.length > 0 && (
                <div className="mt-2">
                  <div className="text-2xs uppercase text-ink-faint mb-0.5">Coordinates</div>
                  {dims.map((d, i) => <KV key={d} k={d} v={hover.coords[i] ?? "—"} />)}
                </div>
              )}
            </div>
          )}
        </Panel>

        <Panel title="Per-island coverage" className="flex-1"
               empty={islandList.length === 0}
               emptyLabel="No island data yet.">
          <Table>
            <thead>
              <tr><Th>Island</Th><Th>Pop</Th><Th>Best</Th><Th>Cells</Th></tr>
            </thead>
            <tbody>
              {islandList.map((i) => (
                <Row key={i.island_id} onClick={() => setIsland(i.island_id)}
                     selected={island === i.island_id}>
                  <Td><Mono>{i.island_id}</Mono></Td>
                  <Td className="tabular">{fmtNum(i.population)}</Td>
                  <Td className="tabular">{fmtScore(i.best_score)}</Td>
                  <Td className="tabular text-ink-faint">
                    {fmtNum(i.metadata?.occupied_cells ?? null)}
                  </Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>

        <Panel title="About this grid">
          <div className="p-2 text-2xs text-ink-dim space-y-1.5">
            <p>
              Cells come from the engine's own per-island feature maps. Each
              island maintains a separate grid, so the same coordinate can hold a
              different elite on each island.
            </p>
            <p>
              Moving the scrubber reconstructs occupancy from recorded
              replacement history rather than re-simulating — what you see is
              what the engine actually held at that generation.
            </p>
          </div>
        </Panel>
      </div>
    </div>
  );
};
