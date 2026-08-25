/** Engine stdout/stderr tail — the real subprocess output. */
import React, { useState } from "react";
import { ViewProps } from "../App";
import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import { Button, Empty, Panel, cx } from "../components/ui";

export const Logs: React.FC<ViewProps> = ({ runId, liveTick }) => {
  const [stream, setStream] = useState<"stdout" | "stderr">("stdout");
  const [lines, setLines] = useState(400);
  const q = useAsync(() => (runId ? api.logs(runId, stream, lines)
                                  : Promise.resolve(null)),
                     [runId, stream, lines, liveTick], 4000);

  if (!runId) return <Panel title="Logs"><Empty>Select a run.</Empty></Panel>;

  const text = q.data?.text ?? "";
  return (
    <Panel title={`Engine ${stream}`} loading={q.loading && !q.data} error={q.error}
           empty={!text}
           emptyLabel={`No ${stream} output recorded yet.`}
           actions={
             <>
               {(["stdout", "stderr"] as const).map((s) => (
                 <Button key={s} size="xs" onClick={() => setStream(s)}>
                   {s === stream ? `● ${s}` : s}
                 </Button>
               ))}
               <select value={lines} onChange={(e) => setLines(Number(e.target.value))}
                       className="bg-surface-2 border border-line rounded px-1 py-0.5 text-2xs">
                 {[200, 400, 1000, 3000].map((n) =>
                   <option key={n} value={n}>{n} lines</option>)}
               </select>
             </>
           }
           footer="Tail of the engine subprocess; refreshes every 4s.">
      <pre className={cx("p-2 text-2xs font-mono whitespace-pre-wrap",
                         stream === "stderr" ? "text-ink-dim" : "text-ink-dim")}>
        {text}
      </pre>
    </Panel>
  );
};
