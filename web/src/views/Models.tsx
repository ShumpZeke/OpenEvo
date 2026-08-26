/**
 * Model / Provider manager (sections 16 & 17).
 *
 * Shows the route table, live health, and the provider doctor's findings.
 * Critically, it shows *why* a model is or is not serving a role — including
 * when the operator's preferred model is excluded for lacking a capability.
 */

import React, {
  useState,
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
  Badge, Button, Mono, Panel, Table, Td, Th, Row, fmtMs, fmtNum, fmtTime,
} from "../components/ui";

export const Models: React.FC<ViewProps> = ({ runId, liveTick }) => {
  const [busy, setBusy] = useState(false);
  const p = useAsync(() => api.providers(), [liveTick]);
  const reqs = useAsync(
    () => (runId ? api.modelRequests(runId, { limit: 150 }) : Promise.resolve(null)),
    [runId, liveTick]);
  // Quality per route, which is a different question from health per route: a
  // route can be perfectly reliable and return nothing but duplicates.
  const quality = useAsync(
    () => (runId ? api.routeQuality(runId) : Promise.resolve(null)),
    [runId, liveTick]);

  const profiles: Json[] = p.data?.profiles ?? [];
  const health: Record<string, Json> = p.data?.health ?? {};
  const routes: Json[] = p.data?.routes ?? [];

  const runDoctor = async () => {
    setBusy(true);
    try { await api.runDoctor(true); p.refresh(); }
    catch (e: any) { alert(e?.message ?? String(e)); }
    finally { setBusy(false); }
  };

  const freeBadge = (s: string) => {
    if (s === "free_limited_time") return <Badge tone="warn" title="Free for a limited time per the provider's own documentation — not guaranteed to remain free">free (limited time)</Badge>;
    if (s === "free") return <Badge tone="ok">free</Badge>;
    if (s === "paid") return <Badge tone="info">paid</Badge>;
    return <Badge tone="stopped" title="Not probed yet — run the provider doctor">unknown</Badge>;
  };

  return (
    <div className="h-full grid grid-rows-[auto_auto_auto_1fr] gap-2 min-h-0
                    overflow-y-auto">
      <Panel title="Model profiles" loading={p.loading && !p.data} error={p.error}
             actions={<Button size="xs" tone="primary" onClick={runDoctor} disabled={busy}>
               {busy ? "probing…" : "run provider doctor"}</Button>}
             footer="Capabilities marked (verified) were measured by a live probe; others are declared defaults.">
        <Table>
          <thead>
            <tr><Th>Profile</Th><Th>Provider</Th><Th>Model</Th><Th>Free status</Th>
                <Th>Capabilities</Th><Th>Credential</Th><Th>Health</Th>
                <Th>Reqs</Th><Th>p50</Th><Th>429</Th><Th></Th></tr>
          </thead>
          <tbody>
            {profiles.map((m) => {
              const h = health[m.id] ?? {};
              const caps = m.verified_capabilities ?? m.declared_capabilities ?? [];
              return (
                <Row key={m.id}>
                  <Td>
                    <Mono className={m.priority === 0 ? "text-accent" : "text-ink"}>
                      {m.id}
                    </Mono>
                    {m.priority === 0 && (
                      <Badge tone="accent" title="Operator's preferred default route">
                        preferred
                      </Badge>
                    )}
                  </Td>
                  <Td><Mono className="text-ink-dim">{m.provider}</Mono></Td>
                  <Td><Mono className="text-ink-dim" title={m.api_base}>{m.model}</Mono></Td>
                  <Td>{freeBadge(m.free_status)}</Td>
                  <Td>
                    <span className="font-mono text-2xs">
                      {caps.join(", ") || "—"}
                    </span>
                    {m.verified_capabilities && (
                      <span className="text-2xs text-ok ml-1">(verified)</span>
                    )}
                  </Td>
                  <Td>{m.secret_ref
                    ? (m.secret_present
                        ? <Badge tone="ok">{m.secret_ref}</Badge>
                        : <Badge tone="warn" title="Set this environment variable to enable the route">
                            {m.secret_ref} missing</Badge>)
                    : <span className="text-ink-faint text-2xs">none needed</span>}</Td>
                  <Td>
                    {h.circuit_open
                      ? <Badge tone="failed" title={h.last_error ?? ""}>circuit open</Badge>
                      : h.total_requests
                        ? <Badge tone={h.success_rate >= 0.9 ? "ok" : "warn"}>
                            {(h.success_rate * 100).toFixed(0)}%
                          </Badge>
                        : <span className="text-ink-faint text-2xs">unused</span>}
                  </Td>
                  <Td className="tabular">{fmtNum(h.total_requests)}</Td>
                  <Td className="tabular">{h.p50_latency_ms ? fmtMs(h.p50_latency_ms) : "—"}</Td>
                  <Td className="tabular">{h.total_rate_limited
                    ? <span className="text-warn">{h.total_rate_limited}</span> : "0"}</Td>
                  <Td>
                    {h.circuit_open && (
                      <Button size="xs" onClick={() =>
                        api.resetCircuit(m.id).then(p.refresh)}>reset</Button>
                    )}
                  </Td>
                </Row>
              );
            })}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Route table"
             footer="A role only routes to a model that satisfies its required capabilities.">
        <Table>
          <thead>
            <tr><Th>Role</Th><Th>Requires</Th><Th>Selected</Th><Th>Fallback chain</Th>
                <Th>Excluded (and why)</Th></tr>
          </thead>
          <tbody>
            {routes.map((r) => (
              <Row key={r.role}>
                <Td><Mono className="text-ink">{r.role}</Mono></Td>
                <Td><span className="font-mono text-2xs text-ink-faint">
                  {(r.required_capabilities ?? []).join(", ")}</span></Td>
                <Td>{r.selected
                  ? <Mono className="text-ok">{r.selected}</Mono>
                  : <Badge tone="warn" title="No configured model can serve this role">
                      no route</Badge>}</Td>
                <Td><span className="font-mono text-2xs text-ink-faint">
                  {(r.chain ?? []).join(" → ")}</span></Td>
                <Td>
                  <div className="space-y-0.5">
                    {Object.entries(r.excluded ?? {}).map(([k, v]) => (
                      <div key={k} className="text-2xs">
                        <span className="font-mono text-ink-dim">{k}</span>
                        <span className="text-ink-faint"> — {String(v)}</span>
                      </div>
                    ))}
                  </div>
                </Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <RouteQuality state={quality} runId={runId} />

      <Panel title={`Model requests${runId ? "" : " (select a run)"}`}
             loading={reqs.loading} error={reqs.error}
             empty={(reqs.data?.model_requests ?? []).length === 0}
             emptyLabel="No model requests recorded for this run.">
        <Table>
          <thead>
            <tr><Th>Time</Th><Th>Provider</Th><Th>Model</Th><Th>Role</Th><Th>Latency</Th>
                <Th>Prompt</Th><Th>Completion</Th><Th>Total</Th><Th>tok/s</Th>
                <Th>Status</Th><Th>Stop</Th></tr>
          </thead>
          <tbody>
            {(reqs.data?.model_requests ?? []).map((m: Json) => (
              <Row key={m.request_id}>
                <Td className="text-ink-faint">{fmtTime(m.started_at)}</Td>
                <Td><Mono>{m.provider ?? "—"}</Mono></Td>
                <Td><Mono className="text-ink-dim">{m.model ?? "—"}</Mono></Td>
                <Td className="text-ink-faint">{m.role ?? "—"}</Td>
                <Td className="tabular">{fmtMs(m.latency_ms)}</Td>
                <Td className="tabular">{fmtNum(m.prompt_tokens)}</Td>
                <Td className="tabular">{fmtNum(m.completion_tokens)}</Td>
                <Td className="tabular">{fmtNum(m.total_tokens)}</Td>
                <Td className="tabular">{m.tokens_per_sec ? m.tokens_per_sec.toFixed(1) : "—"}</Td>
                <Td><Badge tone={m.status}>{m.status}</Badge></Td>
                <Td className="text-ink-faint">{m.stop_reason ?? "—"}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>
    </div>
  );
};


/**
 * Mutation quality per route.
 *
 * The route table above answers "did the route respond?". This answers "did it
 * produce better mutations, and at what cost?" — a different question, and the
 * two were measured coming apart: Ox Alpha at 26% success and a 284 s p50
 * against a fallback at 100% and 112 s. Reliability alone would say "switch".
 *
 * Every number here comes from the run's own telemetry. Where a route has too
 * few attempts to rank, it is listed as excluded with the reason rather than
 * ranked low, and the verdict says plainly when the evidence is too thin to act
 * on — a confident recommendation from four samples would be worse than none.
 */
const pct = (v: unknown): string =>
  typeof v === "number" ? `${(v * 100).toFixed(0)}%` : "—";

const RouteQuality: React.FC<{ state: any; runId: string | null }> = ({ state, runId }) => {
  const data = state.data;
  const routes: Json[] = Object.values(data?.routes ?? {});
  const comparison: Json = data?.comparison ?? {};
  const coverage: Json = data?.coverage ?? {};
  const excluded: Record<string, string> = comparison.excluded_insufficient_data ?? {};

  return (
    <Panel
      title={`Route quality${runId ? "" : " (select a run)"}`}
      loading={state.loading && !data}
      error={state.error}
      empty={routes.length === 0}
      emptyLabel={
        coverage.candidates && !coverage.attributed
          ? "This run has no generation provenance — no route comparison is possible."
          : "No mutation attempts recorded for this run."
      }
      footer={
        <span>
          Every mutation request is charged as an attempt, including ones that
          returned nothing — a route that spends minutes and produces no usable
          diff must not look free.
          {coverage.note ? ` ${coverage.note}.` : ""}
        </span>
      }
    >
      <Table>
        <thead>
          <tr><Th>Route</Th><Th>Attempts</Th><Th>Failed</Th><Th>Unparseable</Th>
              <Th>Duplicates</Th><Th>Valid</Th><Th>Improved</Th><Th>Mean latency</Th>
              <Th>Reasoning</Th><Th>Impr / request</Th><Th>Impr / second</Th></tr>
        </thead>
        <tbody>
          {routes.map((r: Json) => (
            <Row key={r.route}>
              <Td><Mono className="text-ink">{r.route}</Mono></Td>
              <Td className="tabular">{fmtNum(r.attempts)}</Td>
              <Td className="tabular">{fmtNum(r.failures)}</Td>
              <Td className="tabular">{fmtNum(r.unparseable)}</Td>
              <Td className="tabular">{fmtNum(r.duplicates)}</Td>
              <Td className="tabular">{pct(r.validity_rate)}</Td>
              <Td className="tabular">{pct(r.improvement_rate)}</Td>
              <Td className="tabular">
                {typeof r.mean_latency_s === "number" ? `${r.mean_latency_s.toFixed(1)}s` : "—"}
              </Td>
              <Td className="tabular" title="Share of the completion budget spent on hidden reasoning">
                {pct(r.reasoning_share)}
              </Td>
              <Td className="tabular">
                {typeof r.improvement_per_request === "number"
                  ? r.improvement_per_request.toFixed(4) : "—"}
              </Td>
              <Td className="tabular">
                {typeof r.improvement_per_second === "number"
                  ? r.improvement_per_second.toExponential(2) : "—"}
              </Td>
            </Row>
          ))}
        </tbody>
      </Table>

      {(comparison.verdict || Object.keys(excluded).length > 0) && (
        <div className="px-3 py-2 border-t border-line space-y-1">
          {comparison.verdict && (
            <div className="text-2xs text-ink-dim">
              <span className="uppercase tracking-wide text-ink-faint">verdict — </span>
              {comparison.verdict}
            </div>
          )}
          {Object.entries(excluded).map(([route, why]) => (
            <div key={route} className="text-2xs">
              <Badge tone="stopped" title="Too few attempts to rank — excluded rather than ranked low">
                excluded
              </Badge>{" "}
              <span className="font-mono text-ink-dim">{route}</span>
              <span className="text-ink-faint"> — {why}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
};
