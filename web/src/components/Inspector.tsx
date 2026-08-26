/**
 * Candidate Inspector (SOURCE_OF_TRUTH section 11).
 *
 * Tabs cover the full candidate record: code, diff against parent, lineage,
 * evaluation, model calls, MAP-Elites placement, sandbox and raw event payload.
 * Everything shown is fetched from /api/query/.../candidates/{id}; there is no
 * client-side synthesis.
 */

import React, {
  useMemo, useState,
} from "react";
import {
  api, Json,
} from "../lib/api";
import {
  useAsync,
} from "../lib/hooks";
import {
  Badge, Button, Empty, KV, Mono, Table, Td, Th, Row, cx, fmtMs, fmtNum, fmtScore, fmtTime, shortId,
} from "./ui";

type Tab =
  | "overview" | "code" | "diff" | "lineage" | "evaluation" | "model"
  | "verification" | "metrics" | "mapelites" | "sandbox" | "raw";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "code", label: "Code" },
  { id: "diff", label: "Diff" },
  { id: "lineage", label: "Lineage" },
  { id: "evaluation", label: "Evaluation" },
  { id: "model", label: "Model" },
  { id: "verification", label: "Verification" },
  { id: "metrics", label: "Metrics" },
  { id: "mapelites", label: "MAP-Elites" },
  { id: "sandbox", label: "Sandbox" },
  { id: "raw", label: "Raw" },
];

export const Inspector: React.FC<{
  runId: string | null;
  candidateId: string | null;
  onClose: () => void;
  onSelectCandidate: (id: string | null) => void;
}> = ({ runId, candidateId, onClose, onSelectCandidate }) => {
  const [tab, setTab] = useState<Tab>("overview");
  const c = useAsync(
    () => (runId && candidateId ? api.candidate(runId, candidateId)
                                : Promise.resolve(null)),
    [runId, candidateId],
  );
  // Parent is fetched only for the diff tab, where it is actually needed.
  const parentId = c.data?.parent_id ?? c.data?.parents?.[0]?.parent_id ?? null;
  const parent = useAsync(
    () => (runId && parentId && tab === "diff" ? api.candidate(runId, parentId)
                                               : Promise.resolve(null)),
    [runId, parentId, tab],
  );

  const d = c.data;

  return (
    <aside className="w-[420px] shrink-0 border-l border-line bg-surface-1
                      flex flex-col min-h-0">
      <header className="h-8 shrink-0 flex items-center justify-between px-2
                         border-b border-line bg-surface-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-2xs uppercase tracking-wide text-ink-faint">
            Inspector
          </span>
          {candidateId && (
            <Mono className="text-ink truncate" title={candidateId}>
              {shortId(candidateId, 14)}
            </Mono>
          )}
        </div>
        <Button size="xs" onClick={onClose} title="Hide inspector">✕</Button>
      </header>

      {!candidateId ? (
        <Empty>Select a candidate to inspect it.</Empty>
      ) : (
        <>
          <div className="shrink-0 flex flex-wrap gap-0.5 px-1 py-1 border-b
                          border-line bg-surface-1">
            {TABS.map((t) => (
              <button key={t.id} onClick={() => setTab(t.id)}
                      className={cx("px-1.5 py-0.5 text-2xs rounded",
                        tab === t.id ? "bg-surface-3 text-ink"
                                     : "text-ink-faint hover:text-ink")}>
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex-1 min-h-0 overflow-auto p-2">
            {c.error ? (
              <div className="text-xs text-bad font-mono">{c.error}</div>
            ) : c.loading && !d ? (
              <div className="text-xs text-ink-faint">Loading…</div>
            ) : !d ? (
              <Empty>Candidate not found.</Empty>
            ) : (
              <TabBody tab={tab} d={d} parent={parent.data}
                       onSelectCandidate={onSelectCandidate} />
            )}
          </div>
        </>
      )}
    </aside>
  );
};

const TabBody: React.FC<{
  tab: Tab; d: Json; parent: Json | null;
  onSelectCandidate: (id: string) => void;
}> = ({ tab, d, parent, onSelectCandidate }) => {
  switch (tab) {
    case "overview":
      return (
        <div className="space-y-3">
          <div>
            <KV k="candidate" v={<span title={d.candidate_id}>{shortId(d.candidate_id, 16)}</span>} />
            <KV k="parent" v={d.parent_id
              ? <button className="text-info hover:underline"
                        onClick={() => onSelectCandidate(d.parent_id)}>
                  {shortId(d.parent_id, 16)}
                </button>
              : "— (root)"} />
            <KV k="generation" v={fmtNum(d.generation)} />
            <KV k="iteration" v={fmtNum(d.iteration)} />
            <KV k="island" v={d.island_id ?? "—"} />
            <KV k="type" v={d.candidate_type ?? "code"} />
            <KV k="language" v={d.language ?? "—"} />
            <KV k="created" v={fmtTime(d.created_at)} />
            <KV k="combined score" v={fmtScore(d.combined_score)} />
            <KV k="complexity" v={fmtNum(d.complexity, 2)} />
            <KV k="diversity" v={fmtNum(d.diversity, 2)} />
            <KV k="cell" v={d.map_elites_cell ?? "—"} />
            <KV k="code hash" v={d.code_hash ?? "—"} />
            <KV k="code length" v={fmtNum(d.code_length)} />
            <KV k="eval status" v={<Badge tone={d.eval_status ?? "pending"}>
              {d.eval_status ?? "pending"}</Badge>} />
            <KV k="best" v={d.is_best ? <Badge tone="ok">current best</Badge> : "no"} />
          </div>
          {d.changes_summary && (
            <section>
              <h3 className="text-2xs uppercase text-ink-faint mb-1">Mutation summary</h3>
              <p className="text-xs text-ink-dim whitespace-pre-wrap">{d.changes_summary}</p>
            </section>
          )}
        </div>
      );

    case "code":
      return d.code ? (
        <pre className="text-2xs font-mono leading-relaxed whitespace-pre-wrap
                        text-ink-dim">{d.code}</pre>
      ) : (
        <Empty>
          No code recorded for this candidate. Code is captured from the
          candidate.created event; a candidate created before telemetry was
          enabled has none.
        </Empty>
      );

    case "diff":
      return <DiffView current={d.code} parent={parent?.code} parentId={d.parent_id} />;

    case "lineage":
      return (
        <div className="space-y-3">
          <section>
            <h3 className="text-2xs uppercase text-ink-faint mb-1">
              Parents ({d.parents?.length ?? 0})
            </h3>
            {(d.parents ?? []).length === 0 ? (
              <p className="text-xs text-ink-faint">Root candidate — no parents.</p>
            ) : (
              (d.parents ?? []).map((p: Json) => (
                <button key={p.parent_id}
                        onClick={() => onSelectCandidate(p.parent_id)}
                        className="block w-full text-left font-mono text-xs text-info
                                   hover:underline py-0.5">
                  {shortId(p.parent_id, 18)} <span className="text-ink-faint">({p.role})</span>
                </button>
              ))
            )}
          </section>
          <section>
            <h3 className="text-2xs uppercase text-ink-faint mb-1">
              Children ({d.children?.length ?? 0})
            </h3>
            {(d.children ?? []).length === 0 ? (
              <p className="text-xs text-ink-faint">No descendants recorded.</p>
            ) : (
              (d.children ?? []).map((p: Json) => (
                <button key={p.candidate_id}
                        onClick={() => onSelectCandidate(p.candidate_id)}
                        className="block w-full text-left font-mono text-xs text-info
                                   hover:underline py-0.5">
                  {shortId(p.candidate_id, 18)}
                </button>
              ))
            )}
          </section>
        </div>
      );

    case "evaluation":
      return (d.evaluations ?? []).length === 0 ? (
        <Empty>No evaluation recorded for this candidate.</Empty>
      ) : (
        <div className="space-y-3">
          {(d.evaluations ?? []).map((e: Json) => (
            <div key={e.evaluation_id} className="border border-line rounded p-2">
              <div className="flex items-center justify-between mb-1">
                <Badge tone={e.status}>{e.status}</Badge>
                <Mono className="text-ink-faint">{fmtMs(e.duration_ms)}</Mono>
              </div>
              <KV k="evaluator" v={e.evaluator_id ?? "—"} />
              <KV k="stage" v={e.stage ?? 0} />
              <KV k="exit code" v={e.exit_code ?? "—"} />
              <KV k="timed out" v={e.timed_out ? "yes" : "no"} />
              <KV k="score" v={fmtScore(e.combined_score)} />
              {e.failure_class && <KV k="failure" v={e.failure_class} />}
              {Object.keys(e.raw_metrics ?? {}).length > 0 && (
                <div className="mt-1.5">
                  <div className="text-2xs uppercase text-ink-faint mb-0.5">
                    Raw metrics
                  </div>
                  {Object.entries(e.raw_metrics).map(([k, v]) => (
                    <KV key={k} k={k} v={typeof v === "number" ? fmtScore(v) : String(v)} />
                  ))}
                </div>
              )}
              {e.stderr_excerpt && (
                <details className="mt-1.5">
                  <summary className="text-2xs text-ink-faint cursor-pointer">stderr</summary>
                  <pre className="text-2xs font-mono text-bad whitespace-pre-wrap mt-1">
                    {e.stderr_excerpt}
                  </pre>
                </details>
              )}
              {e.stdout_excerpt && (
                <details className="mt-1">
                  <summary className="text-2xs text-ink-faint cursor-pointer">stdout</summary>
                  <pre className="text-2xs font-mono text-ink-dim whitespace-pre-wrap mt-1">
                    {e.stdout_excerpt}
                  </pre>
                </details>
              )}
            </div>
          ))}
        </div>
      );

    case "model":
      return (d.model_requests ?? []).length === 0 ? (
        <Empty>
          No model request produced this candidate. The seed program and
          migrant copies have none by nature; anything else means the run
          predates generation provenance.
        </Empty>
      ) : (
        <Table>
          <thead>
            <tr><Th>Provider</Th><Th>Model</Th><Th>Latency</Th><Th>Tokens</Th>
                <Th>Stop</Th><Th>Status</Th></tr>
          </thead>
          <tbody>
            {(d.model_requests ?? []).map((m: Json) => (
              <Row key={m.request_id}>
                <Td><Mono>{m.provider ?? "—"}</Mono></Td>
                <Td><Mono className="text-ink-dim">{m.model ?? "—"}</Mono></Td>
                <Td className="tabular">{fmtMs(m.latency_ms)}</Td>
                <Td className="tabular">{fmtNum(m.total_tokens)}</Td>
                <Td className="text-ink-faint">{m.stop_reason ?? "—"}</Td>
                <Td><Badge tone={m.status}>{m.status}</Badge></Td>
              </Row>
            ))}
          </tbody>
        </Table>
      );

    case "verification": {
      const checks: Json[] = d.verification ?? [];
      const md: Json = d.metadata ?? {};
      return (
        <div className="space-y-3">
          {/* How this candidate came to exist, before whether it was checked. */}
          <div>
            <p className="text-2xs text-ink-faint mb-1 uppercase tracking-wide">
              Provenance
            </p>
            <KV k="operator" v={d.gen_operator ?? md.generating_operator ?? "—"} />
            <KV k="island policy" v={md.generating_island_policy ?? "—"} />
            <KV k="extra offspring" v={md.multi_offspring ? "yes" : "no"} />
            <KV k="from seed forge"
                v={md.seed_forge ? `yes — ${md.forge_origin ?? "forged"}` : "no"} />
            <KV k="migrant copy" v={md.migrant ? "yes" : "no"} />
          </div>

          {checks.length === 0 ? (
            <Empty>
              Not verified. Verification runs on new champions and on unusually
              large jumps only, and needs OE_MAX_VERIFY set — so "not verified"
              here means "not selected for it", never "checked and fine".
            </Empty>
          ) : (
            <div className="space-y-2">
              {checks.map((c: Json, i: number) => (
                <div key={i} className="rounded border border-line p-2">
                  <div className="flex items-center gap-2">
                    <Badge tone={c.status === "ok" ? "ok"
                      : c.status === "warning" ? "warn" : "bad"}>
                      {c.type?.split(".").pop()}
                    </Badge>
                    <span className="text-2xs text-ink-dim">{c.summary}</span>
                  </div>
                  <div className="mt-1 text-2xs text-ink-faint">
                    {c.trigger ? <>triggered by <Mono>{c.trigger}</Mono> · </> : null}
                    {typeof c.checks_run === "number" ? `${c.checks_run} checks · ` : ""}
                    {c.spec_declared === false
                      ? "this task declares no verification of its own"
                      : null}
                  </div>
                  {(c.failures ?? []).map((f: Json, j: number) => (
                    <div key={j} className="mt-1 text-2xs">
                      <Mono className="text-bad">{f.kind}:{f.name}</Mono>
                      <span className="text-ink-faint"> — {f.message}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      );
    }

    case "metrics": {
      const metrics = d.metrics ?? {};
      return Object.keys(metrics).length === 0 ? (
        <Empty>No metrics recorded.</Empty>
      ) : (
        <div>
          <p className="text-2xs text-ink-faint mb-2">
            Raw evaluator metrics as reported. `combined_score` is the engine's
            own fitness value — the number selection actually used.
          </p>
          {Object.entries(metrics).map(([k, v]) => (
            <KV key={k} k={k} v={typeof v === "number" ? fmtScore(v) : String(v)} />
          ))}
        </div>
      );
    }

    case "mapelites":
      return (
        <div>
          <KV k="cell" v={d.map_elites_cell ?? "— not placed"} />
          <KV k="island" v={d.island_id ?? "—"} />
          <KV k="complexity" v={fmtNum(d.complexity, 3)} />
          <KV k="diversity" v={fmtNum(d.diversity, 3)} />
          <p className="text-2xs text-ink-faint mt-2">
            Each island keeps its own feature map, so this cell coordinate is
            scoped to island {d.island_id ?? "—"}.
          </p>
        </div>
      );

    case "sandbox":
      return (
        <Empty>
          No sandbox run is attached to this candidate. Sandbox execution is an
          optional evaluator backend; it records here once enabled and used.
        </Empty>
      );

    case "raw":
      return (
        <pre className="text-2xs font-mono whitespace-pre-wrap text-ink-dim">
          {JSON.stringify(d, null, 2)}
        </pre>
      );
  }
};

/**
 * Line-level diff against the parent candidate.
 *
 * A longest-common-subsequence diff rather than a naive line-by-line compare:
 * an inserted line at the top would otherwise mark every following line as
 * changed and make the mutation impossible to read.
 */
const DiffView: React.FC<{ current?: string; parent?: string; parentId?: string }> =
  ({ current, parent, parentId }) => {
  const rows = useMemo(() => {
    if (!current || !parent) return null;
    const a = parent.split("\n");
    const b = current.split("\n");
    // LCS table. Candidate programs are small (bounded by max_code_length), so
    // the quadratic table is fine here.
    const n = a.length, m = b.length;
    const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1
                                  : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
    const out: { type: "same" | "add" | "del"; text: string }[] = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { out.push({ type: "same", text: a[i] }); i++; j++; }
      else if (lcs[i + 1][j] >= lcs[i][j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
      else { out.push({ type: "add", text: b[j] }); j++; }
    }
    while (i < n) out.push({ type: "del", text: a[i++] });
    while (j < m) out.push({ type: "add", text: b[j++] });
    return out;
  }, [current, parent]);

  if (!parentId) return <Empty>Root candidate — nothing to diff against.</Empty>;
  if (!current) return <Empty>No code recorded for this candidate.</Empty>;
  if (!parent) return <Empty>Parent code unavailable, so no diff can be shown.</Empty>;
  if (!rows) return <Empty>No diff.</Empty>;

  const changed = rows.filter((r) => r.type !== "same").length;
  return (
    <div>
      <div className="text-2xs text-ink-faint mb-1.5">
        {changed} changed line{changed === 1 ? "" : "s"} vs parent{" "}
        <Mono>{shortId(parentId, 12)}</Mono>
      </div>
      <pre className="text-2xs font-mono leading-relaxed">
        {rows.map((r, idx) => (
          <div key={idx}
               className={cx(
                 "px-1 -mx-1",
                 r.type === "add" && "bg-ok/10 text-ok",
                 r.type === "del" && "bg-bad/10 text-bad",
                 r.type === "same" && "text-ink-faint",
               )}>
            <span className="select-none opacity-50">
              {r.type === "add" ? "+" : r.type === "del" ? "−" : " "}{" "}
            </span>
            {r.text || " "}
          </div>
        ))}
      </pre>
    </div>
  );
};
