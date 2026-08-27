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
  // The broker is a different process and the one that actually routes.
  const broker = useAsync(() => api.broker(), [liveTick]);
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
    <div className="h-full grid grid-rows-[auto_auto_auto_auto_1fr] gap-2 min-h-0
                    overflow-y-auto">
      <BrokerRoutes state={broker} />
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
 * two were measured coming apart: one route at 26% success and a 284 s p50
 * against a fallback at 100% and 112 s. Reliability alone would say "switch".
 *
 * Every number here comes from the run's own telemetry. Where a route has too
 * few attempts to rank, it is listed as excluded with the reason rather than
 * ranked low, and the verdict says plainly when the evidence is too thin to act
 * on — a confident recommendation from four samples would be worse than none.
 */
const pct = (v: unknown): string =>
  typeof v === "number" ? `${(v * 100).toFixed(0)}%` : "—";

/**
 * Live routing state from the OE-MAX broker.
 *
 * The panels below this one describe the control plane's own routing table.
 * The broker is a *different process*, on :8787, and it is the one that
 * actually chooses a provider for every request. Showing only the control
 * plane meant an operator could read a route as healthy here while the broker
 * had its circuit open on it, or see nothing at all for a route the broker had
 * parked because its free allowance was spent.
 *
 * When the broker is not running this says so. It deliberately renders no
 * route table in that case: an operator acting on invented health is worse off
 * than one told the truth that we cannot see.
 */
const BrokerRoutes: React.FC<{ state: any }> = ({ state }) => {
  const data = state.data;
  const reachable: boolean = data?.reachable === true;
  const router: Json = data?.router ?? {};
  const health: Record<string, Json> = router.health ?? {};
  const excluded: Record<string, string> = router.excluded ?? {};
  const eligible: string[] = router.eligible ?? [];
  const stats: Record<string, Json> = data?.stats_by_route ?? {};

  // Every route the broker knows about, whether or not it has served yet.
  const keys = Array.from(new Set([...Object.keys(health), ...Object.keys(stats)]));

  if (state.loading && !data) {
    return <Panel title="Broker routes (live)" loading><span /></Panel>;
  }
  if (!reachable) {
    return (
      <Panel title="Broker routes (live)">
        <div className="px-3 py-2 text-2xs text-ink-faint">
          The OE-MAX broker is not reachable at{" "}
          <Mono className="text-ink-dim">{data?.base ?? "127.0.0.1:8787"}</Mono>.
          Start it with <Mono className="text-ink-dim">./scripts/start-broker.sh</Mono>.
          {data?.detail ? <> <span className="text-ink-faint">({data.detail})</span></> : null}
          <div className="mt-1">
            No route health is shown because none is known — this panel will not
            guess at one.
          </div>
        </div>
      </Panel>
    );
  }

  return (
    <Panel
      title="Broker routes (live)"
      empty={keys.length === 0}
      emptyLabel="The broker is running but has not routed a request yet."
      footer={
        <span>
          The chain the broker will actually try, in order:{" "}
          <Mono className="text-ink-dim">
            {(router.chain ?? []).slice(0, 6).join("  →  ")}
            {(router.chain ?? []).length > 6
              ? `  → +${(router.chain ?? []).length - 6} more` : ""}
          </Mono>
        </span>
      }
    >
      <Table>
        <thead>
          <tr><Th>Route</Th><Th>State</Th><Th>Success</Th><Th>Attempts</Th>
              <Th>p50 latency</Th><Th>Tokens</Th><Th>Last error</Th></tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const h: Json = (health[k]?.health ?? {}) as Json;
            const circuit: Json = (health[k]?.circuit ?? {}) as Json;
            const parked: Json | null = (health[k]?.parked ?? null) as Json | null;
            const st: Json = (stats[k] ?? {}) as Json;
            const serving = eligible.includes(k);

            // Parking outranks the circuit in the display for the same reason
            // it does in the router's own reporting: a parked route's breaker
            // reads "closed", which says the route is fine while it is being
            // skipped.
            const badge = parked
              ? <Badge tone="warn" title={String(parked.reason)}>
                  parked {Math.round(Number(parked.remaining_s ?? 0))}s
                </Badge>
              : circuit.state === "open"
              ? <Badge tone="err" title="Circuit open — the provider is failing">
                  circuit open
                </Badge>
              : circuit.state === "half_open"
              ? <Badge tone="warn">probing</Badge>
              : serving
              ? <Badge tone="ok">serving</Badge>
              : <Badge tone="stopped" title={excluded[k] ?? "not in the eligible set"}>
                  standby
                </Badge>;

            return (
              <Row key={k}>
                <Td><Mono className="text-ink">{k}</Mono></Td>
                <Td>{badge}</Td>
                <Td className="tabular">{pct(h.success_rate)}</Td>
                <Td className="tabular">{fmtNum(h.total_attempts ?? st.requests)}</Td>
                <Td className="tabular">{fmtMs(h.p50_latency_ms)}</Td>
                <Td className="tabular">{fmtNum(h.total_tokens ?? st.tokens)}</Td>
                <Td className="text-ink-faint max-w-[26rem] truncate"
                    title={String(h.last_error ?? "")}>
                  {h.last_error ? String(h.last_error) : "—"}
                </Td>
              </Row>
            );
          })}
        </tbody>
      </Table>

      {Object.keys(excluded).length > 0 && (
        <div className="px-3 py-2 border-t border-line">
          <div className="text-2xs uppercase tracking-wide text-ink-faint mb-1">
            Excluded from the chain, and why
          </div>
          {Object.entries(excluded).map(([k, why]) => (
            <div key={k} className="text-2xs text-ink-faint">
              <Mono className="text-ink-dim">{k}</Mono>{" — "}{why}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
};

const RouteQuality: React.FC<{ state: any; runId: string | null }> = ({ state, runId }) => {
  const data = state.data;
  const routes: Json[] = Object.values(data?.routes ?? {});
  const tp: Json = data?.throughput ?? {};
  const comparison: Json = data?.comparison ?? {};
  const byOperator: Record<string, Json[]> = data?.by_operator ?? {};
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

      {typeof tp.candidates_per_request === "number" && (
        <div className="px-3 py-2 border-t border-line flex flex-wrap gap-x-6 gap-y-1">
          <span className="text-2xs text-ink-faint">
            <span className="uppercase tracking-wide">yield</span>{" "}
            <span className="text-ink-dim">{tp.candidates_per_request.toFixed(2)} per request</span>
          </span>
          <span className="text-2xs text-ink-faint" title="Candidates whose code is distinct from every other candidate in the run. Raw yield that is not distinct is throughput that is not real.">
            <span className="uppercase tracking-wide">distinct</span>{" "}
            <span className="text-ink">
              {typeof tp.useful_candidates_per_request === "number"
                ? `${tp.useful_candidates_per_request.toFixed(2)} per request`
                : "—"}
            </span>
          </span>
          <span className="text-2xs text-ink-faint">
            <span className="uppercase tracking-wide">duplicates</span>{" "}
            <span className={typeof tp.duplicate_share === "number" && tp.duplicate_share > 0.5
              ? "text-warn" : "text-ink-dim"}>
              {typeof tp.duplicate_share === "number" ? pct(tp.duplicate_share) : "—"}
            </span>
          </span>
          {tp.extra_offspring > 0 && (
            <span className="text-2xs text-ink-faint">
              <span className="uppercase tracking-wide">extra offspring</span>{" "}
              <span className="text-ink-dim">{tp.extra_offspring}</span>
            </span>
          )}
        </div>
      )}

      {Object.keys(byOperator).length > 0 && (
        <div className="px-3 py-2 border-t border-line">
          <p className="text-2xs text-ink-faint uppercase tracking-wide mb-1">
            by operator
          </p>
          {/* The nuanced answer this can produce: a slow, strong model may earn
              its latency on RADICAL_RETHINK and waste it on PARAMETER_CHANGE,
              which argues for routing by operator rather than picking one
              winner. Only populated when a run had OE_MAX_OPERATORS set. */}
          <Table>
            <thead>
              <tr><Th>Operator</Th><Th>Route</Th><Th>Attempts</Th><Th>Valid</Th>
                  <Th>Improved</Th><Th>Impr / request</Th></tr>
            </thead>
            <tbody>
              {Object.entries(byOperator).flatMap(([op, rows]) =>
                rows.map((r: Json, i: number) => (
                  <Row key={`${op}-${r.route}`}>
                    <Td>{i === 0 ? <Mono className="text-ink">{op}</Mono> : null}</Td>
                    <Td><Mono className="text-ink-dim">{r.route}</Mono></Td>
                    <Td className="tabular">
                      {fmtNum(r.attempts)}
                      {/* Shown but marked: a table sorted by yield looks
                          equally confident on one attempt as on forty. */}
                      {r.sufficient === false ? (
                        <span className="text-ink-faint"
                              title="too few attempts to read as a finding"> ?</span>
                      ) : null}
                    </Td>
                    <Td className="tabular">{pct(r.validity_rate)}</Td>
                    <Td className="tabular">{pct(r.improvement_rate)}</Td>
                    <Td className={r.sufficient === false
                      ? "tabular text-ink-faint" : "tabular"}>
                      {typeof r.improvement_per_request === "number"
                        ? r.improvement_per_request.toFixed(4) : "—"}
                    </Td>
                  </Row>
                )))}
            </tbody>
          </Table>
          {data?.operator_evidence?.note ? (
            <p className="text-2xs text-ink-faint mt-1">
              ? {data.operator_evidence.note}
            </p>
          ) : null}
        </div>
      )}

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
