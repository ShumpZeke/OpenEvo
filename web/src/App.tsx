/**
 * Control Center shell.
 *
 * Layout follows SOURCE_OF_TRUTH section 9.1: a status bar of live run vitals,
 * a navigation rail, the main workspace, a candidate inspector, and a live
 * event rail along the bottom. Panels collapse and resize; the layout persists.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, subscribeEvents, Json } from "./lib/api";
import { useAsync, useLocalState } from "./lib/hooks";
import { Badge, Button, Mono, cx, fmtNum, fmtScore, shortId } from "./components/ui";
import { CommandPalette } from "./components/CommandPalette";
import { Inspector } from "./components/Inspector";
import { EventRail } from "./components/EventRail";
import { Overview } from "./views/Overview";
import { EvolutionGraph } from "./views/EvolutionGraph";
import { MapElitesLab } from "./views/MapElitesLab";
import { IslandsLab } from "./views/IslandsLab";
import { Candidates } from "./views/Candidates";
import { Experiments } from "./views/Experiments";
import { Models } from "./views/Models";
import { Evaluators } from "./views/Evaluators";
import { AgentSandbox } from "./views/AgentSandbox";
import { Checkpoints } from "./views/Checkpoints";
import { Activity } from "./views/Activity";
import { Metrics } from "./views/Metrics";
import { Logs } from "./views/Logs";
import { Errors } from "./views/Errors";
import { Traces } from "./views/Traces";
import { SystemHealth } from "./views/SystemHealth";
import { ClassicVisualizer } from "./views/ClassicVisualizer";
import { RunComparison } from "./views/RunComparison";
import { Settings } from "./views/Settings";

export type ViewId =
  | "overview" | "evolution" | "map-elites" | "islands" | "candidates"
  | "experiments" | "models" | "evaluators" | "sandbox" | "checkpoints"
  | "activity" | "metrics" | "logs" | "errors" | "traces" | "compare"
  | "system" | "classic" | "settings";

const NAV: { id: ViewId; label: string; group: string }[] = [
  { id: "overview", label: "Overview", group: "Evolution" },
  { id: "evolution", label: "Evolution", group: "Evolution" },
  { id: "map-elites", label: "MAP-Elites", group: "Evolution" },
  { id: "islands", label: "Islands", group: "Evolution" },
  { id: "candidates", label: "Candidates", group: "Evolution" },
  { id: "experiments", label: "Experiments", group: "Control" },
  { id: "models", label: "Models", group: "Control" },
  { id: "evaluators", label: "Evaluators", group: "Control" },
  { id: "sandbox", label: "Agent Sandbox", group: "Control" },
  { id: "checkpoints", label: "Checkpoints", group: "Control" },
  { id: "activity", label: "Activity", group: "Observability" },
  { id: "metrics", label: "Metrics", group: "Observability" },
  { id: "logs", label: "Logs", group: "Observability" },
  { id: "errors", label: "Errors", group: "Observability" },
  { id: "traces", label: "Traces", group: "Observability" },
  { id: "compare", label: "Run Comparison", group: "Observability" },
  { id: "system", label: "System", group: "System" },
  { id: "classic", label: "Classic Visualizer", group: "System" },
  { id: "settings", label: "Settings", group: "System" },
];

export interface ViewProps {
  runId: string | null;
  selectedCandidate: string | null;
  onSelectCandidate: (id: string | null) => void;
  onNavigate: (v: ViewId) => void;
  liveTick: number;
}

export default function App() {
  const [view, setView] = useLocalState<ViewId>("evo.view", "overview");
  const [runId, setRunId] = useLocalState<string | null>("evo.run", null);
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [showInspector, setShowInspector] = useLocalState("evo.inspector", true);
  const [showRail, setShowRail] = useLocalState("evo.rail", true);
  const [connected, setConnected] = useState(false);
  const [dropped, setDropped] = useState(0);
  const [liveEvents, setLiveEvents] = useState<Json[]>([]);
  // Bumped on every live batch so views can cheaply refetch derived state.
  const [liveTick, setLiveTick] = useState(0);

  const runs = useAsync(() => api.runs(), [], 10000);

  // Default to the newest run so a fresh install is not staring at an empty shell.
  useEffect(() => {
    if (!runId && runs.data?.runs?.length) {
      setRunId(runs.data.runs[0].run_id);
    }
  }, [runs.data, runId, setRunId]);

  const summary = useAsync(
    () => (runId ? api.summary(runId) : Promise.resolve(null)),
    [runId, liveTick],
  );
  const runInfo = useAsync(
    () => (runId ? api.run(runId) : Promise.resolve(null)),
    [runId, liveTick],
  );

  useEffect(() => {
    if (!runId) return;
    let pending: Json[] = [];
    let raf = 0;
    const flush = () => {
      raf = 0;
      if (!pending.length) return;
      const batch = pending;
      pending = [];
      // Cap retained events: this rail is a live tail, not a log store.
      setLiveEvents((prev) => [...batch.reverse(), ...prev].slice(0, 500));
      setLiveTick((t) => t + 1);
    };
    return subscribeEvents(
      runId,
      (evs) => {
        pending.push(...evs);
        // Coalesce bursts into one paint; a fast run can emit hundreds a second.
        if (!raf) raf = requestAnimationFrame(flush);
      },
      (n) => setDropped((d) => d + n),
      setConnected,
    );
  }, [runId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
      if (e.key === "Escape") setPaletteOpen(false);
      // Alt+digit jumps between the primary views without leaving the keyboard.
      if (e.altKey && /^[1-9]$/.test(e.key)) {
        const target = NAV[Number(e.key) - 1];
        if (target) setView(target.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setView]);

  const onSelectCandidate = useCallback((id: string | null) => {
    setSelected(id);
    if (id) setShowInspector(true);
  }, [setShowInspector]);

  const viewProps: ViewProps = {
    runId, selectedCandidate: selected, onSelectCandidate,
    onNavigate: setView, liveTick,
  };

  const body = useMemo(() => {
    switch (view) {
      case "overview": return <Overview {...viewProps} />;
      case "evolution": return <EvolutionGraph {...viewProps} />;
      case "map-elites": return <MapElitesLab {...viewProps} />;
      case "islands": return <IslandsLab {...viewProps} />;
      case "candidates": return <Candidates {...viewProps} />;
      case "experiments": return <Experiments {...viewProps} onRunStarted={(id) => {
        setRunId(id); runs.refresh(); setView("overview");
      }} />;
      case "models": return <Models {...viewProps} />;
      case "evaluators": return <Evaluators {...viewProps} />;
      case "sandbox": return <AgentSandbox {...viewProps} />;
      case "checkpoints": return <Checkpoints {...viewProps} />;
      case "activity": return <Activity {...viewProps} liveEvents={liveEvents} />;
      case "metrics": return <Metrics {...viewProps} />;
      case "logs": return <Logs {...viewProps} />;
      case "errors": return <Errors {...viewProps} />;
      case "traces": return <Traces {...viewProps} />;
      case "compare": return <RunComparison {...viewProps} runs={runs.data?.runs ?? []} />;
      case "system": return <SystemHealth {...viewProps} />;
      case "classic": return <ClassicVisualizer {...viewProps} />;
      case "settings": return <Settings {...viewProps} />;
      default: return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, runId, selected, liveEvents, liveTick, runs.data]);

  const s = summary.data;
  const live = runInfo.data?.live;
  const status = live?.status ?? runInfo.data?.status ?? "—";
  const isLive = live?.alive === true;

  return (
    <div className="h-full flex flex-col bg-surface-0">
      {/* ---------------------------------------------------- status bar */}
      <header className="shrink-0 h-9 flex items-center gap-3 px-3 border-b
                         border-line bg-surface-1 text-xs overflow-x-auto">
        <span className="font-semibold tracking-wider text-ink shrink-0">EVOLUTION</span>

        <select
          value={runId ?? ""}
          onChange={(e) => { setRunId(e.target.value || null); setSelected(null); }}
          className="bg-surface-2 border border-line rounded px-1.5 py-0.5 text-xs
                     font-mono max-w-[280px] shrink-0"
          title="Active run"
        >
          <option value="">— select run —</option>
          {(runs.data?.runs ?? []).map((r: Json) => (
            <option key={r.run_id} value={r.run_id}>
              {(r.metadata?.name || r.live?.name || "run")} · {shortId(r.run_id, 10)}
            </option>
          ))}
        </select>

        <div className="flex items-center gap-3 font-mono tabular text-ink-dim shrink-0">
          <StatusChip label="STATUS" value={<Badge tone={status}>{status}</Badge>} />
          <StatusChip label="GEN" value={fmtNum(s?.generation)} />
          <StatusChip label="BEST" value={fmtScore(s?.best?.combined_score)} />
          <StatusChip label="CAND" value={fmtNum(s?.candidates)} />
          <StatusChip label="TOKENS" value={fmtNum(s?.tokens)} />
          <StatusChip label="REQS" value={fmtNum(s?.model_requests)} />
          <StatusChip label="CELLS" value={fmtNum(s?.map_elites_occupied)} />
          <StatusChip label="ISLANDS" value={fmtNum(s?.islands?.length)} />
        </div>

        <div className="flex-1" />

        {dropped > 0 && (
          <span
            className="text-2xs text-warn font-mono shrink-0 cursor-pointer"
            title="The live stream dropped events because this tab fell behind.
Stored history is unaffected — reload a view to see the full record."
            onClick={() => setDropped(0)}
          >
            {fmtNum(dropped)} dropped ✕
          </span>
        )}
        <Button size="xs" onClick={() => setPaletteOpen(true)} title="Search (Ctrl/Cmd+K)">
          ⌕ Search
        </Button>
        <Button size="xs" onClick={() => setShowInspector(!showInspector)}
                title="Toggle inspector">Inspector</Button>
        <Button size="xs" onClick={() => setShowRail(!showRail)}
                title="Toggle live event rail">Events</Button>
        <span className={cx("flex items-center gap-1 font-mono text-2xs shrink-0",
                            connected ? "text-live" : "text-ink-faint")}
              title={connected ? "Live event stream connected"
                               : "Live stream disconnected — retrying"}>
          <span className={cx("w-1.5 h-1.5 rounded-full",
                              connected ? "bg-live" : "bg-ink-faint")} />
          {connected ? (isLive ? "LIVE" : "IDLE") : "OFFLINE"}
        </span>
      </header>

      {/* --------------------------------------------------------- body */}
      <div className="flex-1 flex min-h-0">
        <nav className="w-40 shrink-0 border-r border-line bg-surface-1 overflow-y-auto py-1">
          {Object.entries(
            NAV.reduce<Record<string, typeof NAV>>((acc, item) => {
              (acc[item.group] ||= []).push(item);
              return acc;
            }, {}),
          ).map(([group, items]) => (
            <div key={group} className="mb-1.5">
              <div className="px-3 py-1 text-2xs uppercase tracking-wider text-ink-faint">
                {group}
              </div>
              {items.map((item) => (
                <button
                  key={item.id}
                  onClick={() => setView(item.id)}
                  className={cx(
                    "w-full text-left px-3 py-1 text-xs transition-colors border-l-2",
                    view === item.id
                      ? "bg-surface-3 text-ink border-info"
                      : "text-ink-dim border-transparent hover:bg-surface-2 hover:text-ink",
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="flex-1 flex flex-col min-w-0">
          <main className="flex-1 min-h-0 overflow-hidden p-2">{body}</main>
          {showRail && (
            <EventRail events={liveEvents} runId={runId}
                       onSelectCandidate={onSelectCandidate} />
          )}
        </div>

        {showInspector && (
          <Inspector runId={runId} candidateId={selected}
                     onClose={() => setShowInspector(false)}
                     onSelectCandidate={onSelectCandidate} />
        )}
      </div>

      {paletteOpen && (
        <CommandPalette
          runId={runId}
          onClose={() => setPaletteOpen(false)}
          onSelectCandidate={(id) => { onSelectCandidate(id); setPaletteOpen(false); }}
          onNavigate={(v) => { setView(v); setPaletteOpen(false); }}
          navItems={NAV}
        />
      )}
    </div>
  );
}

const StatusChip: React.FC<{ label: string; value: React.ReactNode }> =
  ({ label, value }) => (
    <span className="flex items-center gap-1 whitespace-nowrap">
      <span className="text-ink-faint text-2xs">{label}</span>
      <Mono className="text-ink">{value}</Mono>
    </span>
  );
