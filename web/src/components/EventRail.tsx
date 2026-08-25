/**
 * Live event rail (the bottom panel of section 9.1).
 *
 * A virtualised tail of the SSE stream. Filterable by family so an operator can
 * watch just evaluations or just model calls during a run.
 */

import React, {
  useMemo, useState,
} from "react";
import {
  Json,
} from "../lib/api";
import {
  cx, fmtMs, fmtTime, shortId,
} from "./ui";

const FAMILIES = [
  "all", "candidate", "evaluator", "model", "map_elites", "island",
  "checkpoint", "sandbox", "resource", "system",
] as const;

export const EventRail: React.FC<{
  events: Json[];
  runId: string | null;
  onSelectCandidate: (id: string) => void;
}> = ({ events, runId, onSelectCandidate }) => {
  const [family, setFamily] = useState<string>("all");
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<Json[]>([]);

  // Pausing snapshots the feed so a fast run cannot scroll a line away while
  // the operator is reading it.
  const source = paused ? frozen : events;
  const shown = useMemo(
    () => (family === "all" ? source
                            : source.filter((e) => String(e.type).startsWith(family))),
    [source, family],
  );

  return (
    <div className="h-48 shrink-0 border-t border-line bg-surface-1 flex flex-col">
      <div className="h-7 shrink-0 flex items-center gap-2 px-2 border-b border-line
                      bg-surface-2 overflow-x-auto">
        <span className="text-2xs uppercase tracking-wide text-ink-faint shrink-0">
          Live events
        </span>
        {FAMILIES.map((f) => (
          <button key={f} onClick={() => setFamily(f)}
                  className={cx("px-1.5 py-0.5 text-2xs rounded font-mono shrink-0",
                    family === f ? "bg-surface-4 text-ink"
                                 : "text-ink-faint hover:text-ink")}>
            {f}
          </button>
        ))}
        <div className="flex-1" />
        <button
          onClick={() => { if (!paused) setFrozen(events); setPaused(!paused); }}
          className={cx("px-1.5 py-0.5 text-2xs rounded border shrink-0",
            paused ? "border-warn/40 text-warn" : "border-line-strong text-ink-dim")}
        >
          {paused ? "paused" : "live"}
        </button>
        <span className="text-2xs text-ink-faint font-mono shrink-0">
          {shown.length}
        </span>
      </div>

      <div className="flex-1 overflow-auto font-mono text-2xs">
        {!runId ? (
          <div className="p-3 text-ink-faint">Select a run to stream its events.</div>
        ) : shown.length === 0 ? (
          <div className="p-3 text-ink-faint">
            No events yet. Start a run, or widen the filter.
          </div>
        ) : (
          <table className="w-full">
            <tbody>
              {shown.slice(0, 300).map((e) => (
                <tr key={e.event_id}
                    className={cx("hover:bg-surface-2/60",
                                  e.candidate_id && "cursor-pointer")}
                    onClick={() => e.candidate_id && onSelectCandidate(e.candidate_id)}>
                  <td className="px-2 py-0.5 text-ink-faint whitespace-nowrap w-20">
                    {fmtTime(e.timestamp)}
                  </td>
                  <td className="px-1 py-0.5 w-56">
                    <span className={cx(
                      "font-mono",
                      e.status === "failed" ? "text-bad"
                        : e.status === "warning" ? "text-warn"
                        : e.type?.startsWith("candidate.best") ? "text-ok"
                        : "text-info")}>
                      {e.type}
                    </span>
                  </td>
                  <td className="px-1 py-0.5 text-ink-faint w-24 truncate">
                    {e.candidate_id ? shortId(e.candidate_id, 10) : ""}
                  </td>
                  <td className="px-1 py-0.5 text-ink-dim truncate">{e.summary}</td>
                  <td className="px-2 py-0.5 text-ink-faint text-right w-16 tabular">
                    {e.duration_ms ? fmtMs(e.duration_ms) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};
