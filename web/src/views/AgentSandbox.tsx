/**
 * Agent Sandbox (section 19).
 *
 * Shows the real isolation posture and any recorded sandbox runs. When the
 * OpenCode backend is unavailable this page says so plainly and explains the
 * fallback, rather than rendering an inert dashboard of zeros.
 */
import React from "react";
import { ViewProps } from "../App";
import { api, Json } from "../lib/api";
import { useAsync } from "../lib/hooks";
import {
  Badge, Empty, KV, Mono, Panel, Table, Td, Th, Row,
} from "../components/ui";

const MODES = [
  ["Direct", "Engine emits a candidate; the sandbox runs the benchmark and returns metrics."],
  ["Agent-realized", "Engine emits a mutation objective; OpenCode/OMO implements it in the sandbox; the evaluator scores the result."],
  ["Agent-harness", "The candidate IS an agent configuration — prompt, skill, workflow or routing policy — scored by running a benchmark suite through it."],
  ["Hybrid", "Program and agent strategy evolve together; scoring covers correctness, latency, token cost and stability."],
];

export const AgentSandbox: React.FC<ViewProps> = () => {
  const sys = useAsync(() => api.system(), [], 10000);
  const iso: Json | undefined = sys.data?.opencode_isolation;
  const enabled = iso?.ok === true;

  return (
    <div className="h-full grid grid-cols-2 gap-2 min-h-0 overflow-auto">
      <Panel title="Sandbox status" loading={sys.loading && !sys.data} error={sys.error}>
        <div className="p-3 space-y-3">
          <div className="flex items-center gap-2">
            <Badge tone={enabled ? "ok" : "warn"}>
              {enabled ? "available" : "disabled"}
            </Badge>
            <Mono className="text-ink-dim">{iso?.level ?? "—"}</Mono>
          </div>

          {!enabled && (
            <div className="p-2 rounded border border-warn/30 bg-warn/5">
              <div className="text-xs text-warn mb-1">
                OpenCode sandbox backend is disabled
              </div>
              <ul className="text-2xs text-ink-dim space-y-0.5">
                {(iso?.reasons ?? []).map((r: string, i: number) => (
                  <li key={i}>• {r}</li>
                ))}
              </ul>
              <p className="text-2xs text-ink-dim mt-1.5">
                Native OpenEvolve evaluation is unaffected and continues to run.
                Disabling this backend is the specified behaviour when isolation
                cannot be guaranteed — Evolution will not fall back to touching
                the operator's own OpenCode installation.
              </p>
            </div>
          )}

          <div>
            <KV k="backend" v={iso?.level ?? "—"} />
            <KV k="opencode binary" v={iso?.binary ?? "not found"} />
            <KV k="binary source" v={iso?.binary_source || "—"} />
            <KV k="container runtime" v={iso?.docker_available ? "docker" : "none"} />
            <KV k="OMO" v={iso?.omo?.available
              ? `${iso.omo.binary} ${iso.omo.version ?? ""}`
              : "not installed (optional)"} />
            <KV k="sandbox root" v={
              <span className="break-all">{iso?.owned_paths?.sandboxes ?? "—"}</span>} />
          </div>

          {iso?.omo && !iso.omo.available && (
            <p className="text-2xs text-ink-faint">
              {iso.omo.note} Probed commands:{" "}
              {(iso.omo.checked ?? []).map((c: Json) => c.command).join(", ")}.
            </p>
          )}
        </div>
      </Panel>

      <Panel title="Evaluation modes"
             footer="Modes become selectable per experiment once a sandbox backend is available.">
        <Table>
          <thead><tr><Th>Mode</Th><Th>Behaviour</Th></tr></thead>
          <tbody>
            {MODES.map(([m, d]) => (
              <Row key={m}>
                <Td><Mono className="text-ink">{m}</Mono></Td>
                <Td className="text-ink-dim">{d}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Sandbox runs" className="col-span-2">
        <Empty>
          No sandbox runs recorded. Sandbox execution is an optional evaluator
          backend; runs appear here once it is enabled and used.
        </Empty>
      </Panel>
    </div>
  );
};
