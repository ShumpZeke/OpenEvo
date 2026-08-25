/**
 * Evolution workspace — interactive lineage graph (section 10).
 *
 * Rendered on a canvas rather than with an SVG/DOM graph library. At the scale
 * section 25 demands (tens of thousands of candidates) one DOM node per
 * candidate stops being viable long before the data does; a canvas draws 20k
 * nodes in a single pass and keeps pan/zoom at frame rate.
 *
 * Layout is deterministic: x by iteration/generation, y by island band then a
 * stable hash of the id. Deterministic placement means a node does not jump
 * between renders as new candidates stream in, which matters when the operator
 * is tracking one lineage during a live run.
 */

import React, {
  useCallback, useEffect, useMemo, useRef, useState,
} from "react";
import {
  ViewProps,
} from "../App";
import {
  api, Json,
} from "../lib/api";
import {
  useAsync, useSize,
} from "../lib/hooks";
import {
  Button, Empty, KV, Panel, fmtNum, fmtScore, scoreColor, shortId,
} from "../components/ui";

interface Node {
  id: string; x: number; y: number; score: number | null;
  gen: number; island: number | null; isBest: boolean; parent: string | null;
  status: string | null;
}

const hash = (s: string): number => {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0) / 4294967295;
};

export const EvolutionGraph: React.FC<ViewProps> = ({
  runId, selectedCandidate, onSelectCandidate, liveTick,
}) => {
  const [containerRef, size] = useSize<HTMLDivElement>();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [hover, setHover] = useState<Node | null>(null);
  const [colorBy, setColorBy] = useState<"score" | "island" | "status">("score");
  const [highlightLineage, setHighlightLineage] = useState(true);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);

  const graph = useAsync(
    () => (runId ? api.lineage(runId, 5000) : Promise.resolve(null)),
    [runId, liveTick],
  );

  const { nodes, byId, bounds } = useMemo(() => {
    const raw = graph.data?.nodes ?? [];
    if (!raw.length) {
      return { nodes: [] as Node[], byId: new Map<string, Node>(),
               bounds: { maxX: 1, maxY: 1, minScore: 0, maxScore: 1 } };
    }
    const scores = raw.map((n: Json) => n.combined_score)
      .filter((s: unknown): s is number => typeof s === "number");
    const minScore = scores.length ? Math.min(...scores) : 0;
    const maxScore = scores.length ? Math.max(...scores) : 1;
    const islands = new Set(raw.map((n: Json) => n.island_id ?? 0));
    const islandCount = Math.max(1, islands.size);
    const maxIter = Math.max(1, ...raw.map((n: Json) => n.iteration ?? n.generation ?? 0));

    const list: Node[] = raw.map((n: Json) => {
      const iter = n.iteration ?? n.generation ?? 0;
      const island = n.island_id ?? 0;
      // Island bands stacked vertically; jitter inside a band keeps siblings
      // from overlapping without randomising position between frames.
      const band = 1 / islandCount;
      const y = band * island + band * (0.15 + 0.7 * hash(n.candidate_id));
      return {
        id: n.candidate_id,
        x: iter / maxIter,
        y,
        score: typeof n.combined_score === "number" ? n.combined_score : null,
        gen: n.generation ?? 0,
        island: n.island_id ?? null,
        isBest: !!n.is_best,
        parent: n.parent_id ?? null,
        status: n.eval_status ?? null,
      };
    });
    return {
      nodes: list,
      byId: new Map(list.map((n) => [n.id, n])),
      bounds: { maxX: 1, maxY: 1, minScore, maxScore },
    };
  }, [graph.data]);

  const edges = graph.data?.edges ?? [];

  // Ancestors + descendants of the selection, for lineage highlighting.
  const lineageSet = useMemo(() => {
    if (!selectedCandidate || !highlightLineage) return null;
    const parents = new Map<string, string[]>();
    const children = new Map<string, string[]>();
    for (const e of edges) {
      (parents.get(e.candidate_id) ?? parents.set(e.candidate_id, []).get(e.candidate_id)!)
        .push(e.parent_id);
      (children.get(e.parent_id) ?? children.set(e.parent_id, []).get(e.parent_id)!)
        .push(e.candidate_id);
    }
    const seen = new Set<string>([selectedCandidate]);
    const walk = (start: string, map: Map<string, string[]>) => {
      const stack = [start];
      while (stack.length) {
        const cur = stack.pop()!;
        for (const nxt of map.get(cur) ?? []) {
          if (!seen.has(nxt)) { seen.add(nxt); stack.push(nxt); }
        }
      }
    };
    walk(selectedCandidate, parents);
    walk(selectedCandidate, children);
    return seen;
  }, [selectedCandidate, edges, highlightLineage]);

  const project = useCallback((n: Node, w: number, h: number) => ({
    px: (n.x * (w - 60) + 30) * view.k + view.x,
    py: (n.y * (h - 40) + 20) * view.k + view.y,
  }), [view]);

  const nodeColor = useCallback((n: Node): string => {
    if (colorBy === "island") {
      const palette = ["#60a5fa", "#4ade80", "#fbbf24", "#f472b6", "#a78bfa",
                       "#22d3ee", "#fb923c", "#94a3b8"];
      return palette[(n.island ?? 0) % palette.length];
    }
    if (colorBy === "status") {
      return n.status === "failed" ? "#f87171"
        : n.status === "ok" ? "#4ade80"
        : "#8f9bb0";
    }
    return scoreColor(n.score, bounds.minScore, bounds.maxScore);
  }, [colorBy, bounds]);

  // -- draw ---------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !size.width || !size.height) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.width * dpr;
    canvas.height = size.height * dpr;
    canvas.style.width = `${size.width}px`;
    canvas.style.height = `${size.height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.width, size.height);
    ctx.fillStyle = "#0a0c10";
    ctx.fillRect(0, 0, size.width, size.height);

    // edges first, so nodes sit on top
    ctx.lineWidth = 1;
    for (const e of edges) {
      const a = byId.get(e.parent_id);
      const b = byId.get(e.candidate_id);
      if (!a || !b) continue;
      const inLineage = !lineageSet || (lineageSet.has(a.id) && lineageSet.has(b.id));
      const pa = project(a, size.width, size.height);
      const pb = project(b, size.width, size.height);
      ctx.strokeStyle = inLineage ? "rgba(96,165,250,0.45)" : "rgba(58,67,86,0.18)";
      ctx.beginPath();
      ctx.moveTo(pa.px, pa.py);
      // Gentle curve makes parallel edges distinguishable at density.
      const mx = (pa.px + pb.px) / 2;
      ctx.bezierCurveTo(mx, pa.py, mx, pb.py, pb.px, pb.py);
      ctx.stroke();
    }

    const r = Math.max(1.5, 3 * Math.min(2, view.k));
    for (const n of nodes) {
      const p = project(n, size.width, size.height);
      if (p.px < -20 || p.px > size.width + 20 || p.py < -20 || p.py > size.height + 20) {
        continue; // cull offscreen
      }
      const dim = lineageSet && !lineageSet.has(n.id);
      ctx.globalAlpha = dim ? 0.18 : 1;
      ctx.fillStyle = nodeColor(n);
      ctx.beginPath();
      ctx.arc(p.px, p.py, n.isBest ? r * 2 : r, 0, Math.PI * 2);
      ctx.fill();
      if (n.isBest) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = "#4ade80";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (n.id === selectedCandidate) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.px, p.py, r * 2.6, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }, [nodes, edges, byId, size, view, selectedCandidate, lineageSet, project, nodeColor]);

  // -- interaction ---------------------------------------------------
  const pick = (clientX: number, clientY: number): Node | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const mx = clientX - rect.left, my = clientY - rect.top;
    let best: Node | null = null, bestD = 12 * 12;
    for (const n of nodes) {
      const p = project(n, size.width, size.height);
      const d = (p.px - mx) ** 2 + (p.py - my) ** 2;
      if (d < bestD) { bestD = d; best = n; }
    }
    return best;
  };

  const fit = () => setView({ x: 0, y: 0, k: 1 });

  if (!runId) return <Panel title="Evolution"><Empty>Select a run.</Empty></Panel>;

  return (
    <div className="h-full grid grid-cols-[1fr_260px] gap-2 min-h-0">
      <Panel
        title="Lineage graph"
        loading={graph.loading && !graph.data}
        error={graph.error}
        empty={nodes.length === 0}
        emptyLabel="No candidates recorded for this run yet."
        bodyClassName="p-0"
        actions={
          <>
            <select value={colorBy} onChange={(e) => setColorBy(e.target.value as any)}
                    className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
              <option value="score">colour: score</option>
              <option value="island">colour: island</option>
              <option value="status">colour: eval status</option>
            </select>
            <Button size="xs" onClick={() => setHighlightLineage(!highlightLineage)}
                    title="Dim everything outside the selected candidate's ancestry">
              {highlightLineage ? "lineage ✓" : "lineage"}
            </Button>
            <Button size="xs" onClick={() => setView((v) => ({ ...v, k: v.k * 1.3 }))}>+</Button>
            <Button size="xs" onClick={() => setView((v) => ({ ...v, k: v.k / 1.3 }))}>−</Button>
            <Button size="xs" onClick={fit}>fit</Button>
          </>
        }
        footer={
          graph.data?.truncated
            ? `showing ${fmtNum(nodes.length)} of ${fmtNum(graph.data.total)} candidates — server-capped for responsiveness`
            : `${fmtNum(nodes.length)} candidates · ${fmtNum(edges.length)} lineage edges`
        }
      >
        <div ref={containerRef} className="relative w-full h-full min-h-[300px]">
          <canvas
            ref={canvasRef}
            className="absolute inset-0 cursor-crosshair"
            onMouseDown={(e) => {
              drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
            }}
            onMouseUp={() => { drag.current = null; }}
            onMouseLeave={() => { drag.current = null; setHover(null); }}
            onMouseMove={(e) => {
              if (drag.current) {
                setView((v) => ({
                  ...v,
                  x: drag.current!.vx + (e.clientX - drag.current!.x),
                  y: drag.current!.vy + (e.clientY - drag.current!.y),
                }));
              } else {
                setHover(pick(e.clientX, e.clientY));
              }
            }}
            onClick={(e) => {
              const n = pick(e.clientX, e.clientY);
              if (n) onSelectCandidate(n.id);
            }}
            onWheel={(e) => {
              const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
              setView((v) => ({ ...v, k: Math.max(0.2, Math.min(12, v.k * factor)) }));
            }}
          />
          {hover && (
            <div className="absolute pointer-events-none bg-surface-3 border border-line-strong
                            rounded px-2 py-1 text-2xs font-mono shadow-lg z-10"
                 style={{
                   left: Math.min(project(hover, size.width, size.height).px + 12,
                                  size.width - 200),
                   top: Math.min(project(hover, size.width, size.height).py + 12,
                                 size.height - 100),
                 }}>
              <div className="text-ink">{shortId(hover.id, 16)}</div>
              <div className="text-ink-dim">score {fmtScore(hover.score)}</div>
              <div className="text-ink-dim">gen {hover.gen} · island {hover.island ?? "—"}</div>
              {hover.isBest && <div className="text-ok">★ current best</div>}
            </div>
          )}
        </div>
      </Panel>

      <div className="flex flex-col gap-2 min-h-0">
        <Panel title="Legend">
          <div className="p-2 space-y-2">
            <div>
              <div className="text-2xs uppercase text-ink-faint mb-1">
                {colorBy === "score" ? "Fitness" : colorBy === "island" ? "Island" : "Eval status"}
              </div>
              {colorBy === "score" ? (
                <>
                  <div className="h-2 rounded" style={{
                    background: `linear-gradient(to right, ${scoreColor(bounds.minScore,
                      bounds.minScore, bounds.maxScore)}, ${scoreColor(bounds.maxScore,
                      bounds.minScore, bounds.maxScore)})`,
                  }} />
                  <div className="flex justify-between text-2xs text-ink-faint mt-0.5">
                    <span>{fmtScore(bounds.minScore)}</span>
                    <span>{fmtScore(bounds.maxScore)}</span>
                  </div>
                </>
              ) : (
                <div className="text-2xs text-ink-dim">
                  {colorBy === "island"
                    ? "One colour per island; vertical bands group islands."
                    : "green = evaluated ok, red = failed, grey = pending"}
                </div>
              )}
            </div>
            <div className="text-2xs text-ink-faint space-y-0.5">
              <div>drag to pan · scroll to zoom · click a node to inspect</div>
              <div>x = iteration · y = island band</div>
              <div>★ larger green ring marks the current best</div>
            </div>
          </div>
        </Panel>

        <Panel title="Selection" className="flex-1">
          {!selectedCandidate ? (
            <Empty>Click a node to select it.</Empty>
          ) : (
            <div className="p-2">
              <KV k="candidate" v={shortId(selectedCandidate, 16)} />
              {(() => {
                const n = byId.get(selectedCandidate);
                if (!n) return <div className="text-2xs text-ink-faint mt-2">
                  Not in the current graph window.</div>;
                return (
                  <>
                    <KV k="score" v={fmtScore(n.score)} />
                    <KV k="generation" v={n.gen} />
                    <KV k="island" v={n.island ?? "—"} />
                    <KV k="parent" v={n.parent ? shortId(n.parent, 12) : "root"} />
                    {lineageSet && (
                      <KV k="lineage size" v={`${lineageSet.size} candidates`} />
                    )}
                  </>
                );
              })()}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
};
