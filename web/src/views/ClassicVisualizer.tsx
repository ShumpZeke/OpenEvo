/**
 * Classic Visualizer bridge (section 6 of the architect prompt).
 *
 * The original OpenEvolve visualizer is preserved exactly as upstream ships it
 * — a separate Flask app reading checkpoints. It is not reimplemented and not
 * replaced; this page tells the operator how to reach it and which checkpoints
 * are available to feed it.
 */
import React from "react";
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
  Badge, KV, Mono, Panel, Table, Td, Th, Row, fmtNum,
} from "../components/ui";

export const ClassicVisualizer: React.FC<ViewProps> = () => {
  const q = useAsync(() => api.classic(), []);
  const d = q.data;

  return (
    <div className="h-full grid grid-cols-2 gap-2 min-h-0">
      <Panel title="Classic OpenEvolve visualizer"
             loading={q.loading && !q.data} error={q.error}>
        <div className="p-3 space-y-3">
          <div className="flex items-center gap-2">
            <Badge tone={d?.available ? "ok" : "warn"}>
              {d?.available ? "present" : "missing"}
            </Badge>
            <span className="text-2xs text-ink-dim">
              {d?.available
                ? "Upstream's own visualizer, unmodified."
                : "scripts/visualizer.py was not found."}
            </span>
          </div>
          <KV k="script" v={<span className="break-all">{d?.script ?? "—"}</span>} />
          <KV k="default url" v={d?.url ?? "—"} />
          <KV k="port" v={d?.port ?? "—"} />

          <div>
            <div className="text-2xs uppercase text-ink-faint mb-1">Launch</div>
            <pre className="text-2xs font-mono bg-surface-2 rounded p-2 text-ink-dim
                            whitespace-pre-wrap">
{`# from the repository root
.venv/bin/python scripts/visualizer.py --path <checkpoint_dir> --port ${d?.port ?? 8080}

# or use the helper
./run.sh classic <checkpoint_dir>`}
            </pre>
          </div>

          {d?.url && (
            <a href={d.url} target="_blank" rel="noreferrer"
               className="inline-block text-info hover:underline text-xs">
              Open {d.url} ↗
            </a>
          )}

          <p className="text-2xs text-ink-faint leading-relaxed">
            The classic UI runs as its own service on its own port so that it
            keeps working exactly as before, whether or not the Control Center is
            running. Preserving it is a hard requirement, not a compatibility
            shim.
          </p>
        </div>
      </Panel>

      <Panel title="Runs with checkpoints"
             empty={(d?.runs_with_checkpoints ?? []).length === 0}
             emptyLabel="No run has produced a checkpoint yet.">
        <Table>
          <thead>
            <tr><Th>Run</Th><Th>Name</Th><Th>Checkpoints</Th><Th>Checkpoint root</Th></tr>
          </thead>
          <tbody>
            {(d?.runs_with_checkpoints ?? []).map((r: Json) => (
              <Row key={r.run_id}>
                <Td><Mono>{String(r.run_id).slice(0, 12)}</Mono></Td>
                <Td>{r.name}</Td>
                <Td className="tabular">{fmtNum(r.checkpoints)}</Td>
                <Td><Mono className="text-2xs text-ink-faint break-all">
                  {r.checkpoint_root}</Mono></Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>
    </div>
  );
};
