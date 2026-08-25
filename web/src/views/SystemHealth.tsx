/**
 * System Health (sections 30 & 32) — including the OpenCode isolation report,
 * which must state exactly what Evolution owns and what it never touches.
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
  Badge, Empty, KV, Mono, Panel, Stat, fmtNum, shortId,
} from "../components/ui";

export const SystemHealth: React.FC<ViewProps> = () => {
  const sys = useAsync(() => api.system(), [], 5000);
  const health = useAsync(() => api.health(), [], 5000);

  const s = sys.data;
  const iso: Json | undefined = s?.opencode_isolation;
  const bus: Json | undefined = s?.telemetry_bus;
  const col: Json | undefined = s?.collector;

  return (
    <div className="h-full grid grid-cols-2 gap-2 min-h-0 overflow-auto">
      <Panel title="Host" loading={sys.loading && !sys.data} error={sys.error}>
        {!s?.host ? (
          <div className="p-3 text-xs text-ink-faint">
            Host metrics unavailable{s?.host_error ? `: ${s.host_error}` : ""}.
            Values are omitted rather than shown as zero.
          </div>
        ) : (
          <div className="grid grid-cols-3 divide-x divide-line">
            <Stat label="CPU" value={`${s.host.cpu_percent?.toFixed(0)}%`}
                  sub={`${s.host.cpu_count} cores`} />
            <Stat label="RAM" value={`${s.host.ram_percent?.toFixed(0)}%`}
                  sub={`${fmtNum(s.host.ram_used_mb)} / ${fmtNum(s.host.ram_total_mb)} MiB`} />
            <Stat label="Disk" value={`${s.host.disk_percent?.toFixed(0)}%`}
                  sub={`${s.host.disk_free_gb} GiB free`} />
          </div>
        )}
      </Panel>

      <Panel title="Telemetry self-health"
             footer="A non-zero drop count means the UI's live tail is incomplete; stored history still is not.">
        <div className="p-2">
          {bus ? (
            <>
              <KV k="events emitted" v={fmtNum(bus.emitted)} />
              <KV k="delivered" v={fmtNum(bus.delivered)} />
              <KV k="queue depth" v={`${fmtNum(bus.queue_depth)} / ${fmtNum(bus.queue_capacity)}`} />
              <KV k="peak depth" v={fmtNum(bus.max_queue_depth)} />
              <KV k="dropped (overflow)" v={
                <span className={bus.dropped_overflow ? "text-warn" : ""}>
                  {fmtNum(bus.dropped_overflow)}</span>} />
              <KV k="dropped (sampled)" v={fmtNum(bus.dropped_sampled)} />
              <KV k="drop ratio" v={String(bus.drop_ratio ?? 0)} />
              <KV k="sink errors" v={
                <span className={bus.sink_errors ? "text-bad" : ""}>
                  {fmtNum(bus.sink_errors)}</span>} />
              <KV k="emit rate" v={`${bus.emit_rate_per_s ?? 0}/s`} />
              {bus.last_error && <KV k="last error" v={String(bus.last_error)} />}
            </>
          ) : <Empty>No telemetry bus in this process.</Empty>}
          {col && (
            <div className="mt-2 pt-2 border-t border-line">
              <div className="text-2xs uppercase text-ink-faint mb-1">Collector</div>
              <KV k="received" v={fmtNum(col.received)} />
              <KV k="ingested" v={fmtNum(col.ingested)} />
              <KV k="duplicates skipped" v={fmtNum(col.duplicates)} />
              <KV k="parse errors" v={fmtNum(col.parse_errors)} />
              <KV k="ingest errors" v={fmtNum(col.ingest_errors)} />
              <KV k="pending" v={fmtNum(col.pending)} />
              <KV k="subscribers" v={fmtNum(col.subscribers)} />
              <KV k="tailed logs" v={fmtNum((col.tailed_logs ?? []).length)} />
            </div>
          )}
        </div>
      </Panel>

      <Panel title="OpenCode isolation"
             footer="Evolution redirects HOME and every XDG path into its own tree, so it cannot read or write the operator's OpenCode state.">
        <div className="p-2">
          {!iso ? <Empty>Unavailable.</Empty> : (
            <>
              <div className="flex items-center gap-2 mb-2">
                <Badge tone={iso.ok ? "ok" : "warn"}>{iso.level}</Badge>
                <span className="text-2xs text-ink-dim">
                  {iso.ok ? "isolation active" : "sandbox backend disabled"}
                </span>
              </div>
              <KV k="binary" v={iso.binary ?? "not found"} />
              <KV k="binary source" v={iso.binary_source || "—"} />
              <KV k="version" v={iso.binary_version ?? "—"} />
              <KV k="docker" v={iso.docker_available ? "available" : "not available"} />
              <KV k="OMO" v={iso.omo?.available
                ? `${iso.omo.binary} ${iso.omo.version ?? ""}` : "not installed (optional)"} />
              <KV k="owned root" v={<span className="break-all">{iso.root}</span>} />

              {(iso.reasons ?? []).length > 0 && (
                <div className="mt-2">
                  <div className="text-2xs uppercase text-ink-faint mb-0.5">Reasons</div>
                  {iso.reasons.map((r: string, i: number) => (
                    <div key={i} className="text-2xs text-warn">• {r}</div>
                  ))}
                </div>
              )}
              {(iso.warnings ?? []).length > 0 && (
                <div className="mt-2">
                  <div className="text-2xs uppercase text-ink-faint mb-0.5">Notes</div>
                  {iso.warnings.map((w: string, i: number) => (
                    <div key={i} className="text-2xs text-ink-dim">• {w}</div>
                  ))}
                </div>
              )}
              <details className="mt-2">
                <summary className="text-2xs text-ink-faint cursor-pointer">
                  Paths Evolution owns ({Object.keys(iso.owned_paths ?? {}).length})
                </summary>
                <div className="mt-1">
                  {Object.entries(iso.owned_paths ?? {}).map(([k, v]) => (
                    <KV key={k} k={k} v={<span className="break-all">{String(v)}</span>} />
                  ))}
                </div>
              </details>
              <details className="mt-1">
                <summary className="text-2xs text-ink-faint cursor-pointer">
                  Paths Evolution never touches ({(iso.never_touched ?? []).length})
                </summary>
                <div className="mt-1">
                  {(iso.never_touched ?? []).map((p: string) => (
                    <div key={p} className="text-2xs font-mono text-ink-faint break-all">
                      {p}
                    </div>
                  ))}
                </div>
              </details>
            </>
          )}
        </div>
      </Panel>

      <Panel title="Upstream & capabilities">
        <div className="p-2">
          {s?.upstream ? (
            <>
              <KV k="upstream" v={s.upstream.upstream_repo} />
              <KV k="commit" v={shortId(s.upstream.upstream_commit, 12)} />
              <KV k="version" v={s.upstream.upstream_version} />
              <KV k="license" v={s.upstream.upstream_license} />
              <KV k="baseline tests" v={`${s.upstream.baseline_tests?.passed} passed`} />
            </>
          ) : <div className="text-2xs text-ink-faint">No upstream manifest found.</div>}
          <div className="mt-2 pt-2 border-t border-line">
            <div className="text-2xs uppercase text-ink-faint mb-1">Run controls</div>
            {Object.entries(health.data?.capabilities ?? s?.runner_capabilities ?? {})
              .map(([k, v]: [string, any]) => (
                <div key={k} className="flex items-start gap-2 py-0.5">
                  <Badge tone={v.supported ? "ok" : "stopped"}>
                    {v.supported ? "yes" : "no"}
                  </Badge>
                  <div className="min-w-0">
                    <Mono className="text-ink">{k}</Mono>
                    {v.reason && (
                      <div className="text-2xs text-ink-faint">{v.reason}</div>
                    )}
                  </div>
                </div>
              ))}
          </div>
        </div>
      </Panel>
    </div>
  );
};
