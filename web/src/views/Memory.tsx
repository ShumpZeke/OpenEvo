/**
 * Memory — where you left off.
 *
 * Answers the question someone has after a week away: what did I run, how did
 * it go, what can I pick up, and what did I write down.
 *
 * Everything except the journal is DERIVED at read time from the same
 * projections the rest of the Control Center serves, so this view cannot drift
 * from the run list beside it. The journal is the one thing stored, because
 * *why* a decision was made was never an event and cannot be reconstructed
 * from one.
 */

import React, { useState } from "react";
import { ViewProps } from "../App";
import { api, Json } from "../lib/api";
import { useAsync } from "../lib/hooks";
import {
  Badge, Button, Mono, Panel, Row, Table, Td, Th, fmtNum, fmtTime,
} from "../components/ui";

const KIND_TONE: Record<string, "ok" | "warn" | "err" | "info" | "stopped"> = {
  milestone: "ok",
  decision: "info",
  blocker: "err",
  session: "warn",
  note: "stopped",
};

export const Memory: React.FC<ViewProps> = ({ liveTick, onNavigate }) => {
  const digest = useAsync(() => api.memory(), [liveTick]);
  const [title, setTitle] = useState("");
  const [detail, setDetail] = useState("");
  const [kind, setKind] = useState("note");
  const [busy, setBusy] = useState(false);

  const d = digest.data;
  const totals: Json = d?.totals ?? {};
  const resumable: Json[] = d?.resumable ?? [];
  const recent: Json[] = d?.recent_runs ?? [];
  const journal: Json[] = d?.journal ?? [];
  const best = d?.best_ever ?? undefined;
  const unfinished: Json[] = d?.unfinished ?? [];

  const addNote = async () => {
    if (!title.trim()) return;
    setBusy(true);
    try {
      await api.addJournalEntry({ title, detail, kind });
      setTitle(""); setDetail("");
      digest.refresh();
    } catch (e: any) {
      alert(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="h-full grid grid-rows-[auto_auto_1fr] gap-2 min-h-0 overflow-y-auto">
      <Panel
        title="Where you left off"
        loading={digest.loading && !d}
        error={digest.error}
        footer="Run history is derived from the event log, so it cannot disagree with the run list. Runs started from the shell are imported on load."
      >
        {!totals.runs ? (
          <div className="p-3 text-xs text-ink-faint">
            No runs recorded in this workspace yet. Start one from{" "}
            <button className="underline" onClick={() => onNavigate("experiments")}>
              Experiments
            </button>
            , or run <Mono className="text-ink-dim">./scripts/run-evolution.sh</Mono>.
          </div>
        ) : (
          <div className="grid grid-cols-4 divide-x divide-line">
            <div className="p-2">
              <div className="text-2xs uppercase tracking-wide text-ink-faint">Runs</div>
              <div className="text-lg text-ink tabular">{fmtNum(totals.runs)}</div>
            </div>
            <div className="p-2">
              <div className="text-2xs uppercase tracking-wide text-ink-faint">Iterations</div>
              {/* null, not 0, when nothing has run — "no data" and "zero
                  iterations across four runs" are different facts. */}
              <div className="text-lg text-ink tabular">
                {totals.iterations === null || totals.iterations === undefined
                  ? "—" : fmtNum(totals.iterations)}
              </div>
            </div>
            <div className="p-2">
              <div className="text-2xs uppercase tracking-wide text-ink-faint">Best ever</div>
              <div className="text-lg text-ink tabular">
                {best && typeof best.best_fitness === "number"
                  ? best.best_fitness.toFixed(4) : "—"}
              </div>
            </div>
            <div className="p-2">
              <div className="text-2xs uppercase tracking-wide text-ink-faint">In progress</div>
              <div className="text-lg text-ink tabular">{unfinished.length}</div>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title={`Resumable runs${resumable.length ? ` (${resumable.length})` : ""}`}
        empty={resumable.length === 0}
        emptyLabel={
          totals.runs
            ? "No run has written a checkpoint yet. Checkpoints are written every `checkpoint_interval` iterations (6 by default), so short runs have none."
            : "Nothing to resume yet."
        }
        footer="Copy the command to continue a run from its newest checkpoint."
      >
        <Table>
          <thead>
            <tr><Th>Run</Th><Th>Task</Th><Th>Checkpoint</Th><Th>Best</Th>
                <Th>Status</Th><Th>Resume with</Th></tr>
          </thead>
          <tbody>
            {resumable.map((p) => (
              <Row key={String(p.run_id)}>
                <Td><Mono className="text-ink">{p.output_dir ?? p.run_id}</Mono></Td>
                {/* The task is inferred from the directory name. When it
                    cannot be, say so rather than guessing: resuming with the
                    wrong evaluator produces confident scores for a different
                    problem. */}
                <Td>{p.task ? String(p.task) : <span className="text-ink-faint">unknown</span>}</Td>
                <Td className="tabular">{fmtNum(p.checkpoint_iteration)}</Td>
                <Td className="tabular">
                  {typeof p.best_fitness === "number" ? p.best_fitness.toFixed(4) : "—"}
                </Td>
                <Td><Badge tone={p.status === "completed" ? "ok" : "warn"}>{String(p.status)}</Badge></Td>
                <Td>
                  {p.resume_command
                    ? <Mono className="text-ink-dim text-2xs">{String(p.resume_command)}</Mono>
                    : <span className="text-ink-faint">—</span>}
                </Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel
        title="Journal"
        actions={
          <div className="flex gap-1 items-center">
            <select
              className="bg-surface border border-line text-2xs px-1 py-0.5 text-ink"
              value={kind} onChange={(e) => setKind(e.target.value)}
            >
              {["note", "decision", "blocker", "milestone", "session"].map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
            <input
              className="bg-surface border border-line text-2xs px-1 py-0.5 text-ink w-56"
              placeholder="what you want to remember"
              value={title} onChange={(e) => setTitle(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") addNote(); }}
            />
            <input
              className="bg-surface border border-line text-2xs px-1 py-0.5 text-ink w-56"
              placeholder="detail (optional)"
              value={detail} onChange={(e) => setDetail(e.target.value)}
            />
            <Button size="xs" tone="primary" onClick={addNote}
                    disabled={busy || !title.trim()}>
              {busy ? "saving…" : "record"}
            </Button>
          </div>
        }
        empty={journal.length === 0}
        emptyLabel="Nothing recorded yet. The journal holds what the event log cannot reconstruct — why a decision was made, what you were in the middle of."
        footer="Entries marked 'you' were written by a person; 'agent' entries were recorded by a program. The distinction is kept so an assertion is never mistaken for an inference."
      >
        <Table>
          <thead>
            <tr><Th>Kind</Th><Th>When</Th><Th>Title</Th><Th>Detail</Th><Th>By</Th></tr>
          </thead>
          <tbody>
            {journal.map((e) => (
              <Row key={String(e.entry_id)}>
                <Td><Badge tone={KIND_TONE[String(e.kind)] ?? "stopped"}>{String(e.kind)}</Badge></Td>
                <Td className="text-ink-faint">{fmtTime(e.created_at)}</Td>
                <Td className="text-ink">{String(e.title)}</Td>
                <Td className="text-ink-faint max-w-[28rem] truncate" title={String(e.detail ?? "")}>
                  {e.detail ? String(e.detail) : "—"}
                </Td>
                <Td className="text-ink-faint">{e.source === "user" ? "you" : "agent"}</Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      {recent.length > 0 && (
        <Panel title="Recent runs" footer="Derived from the event log, not a separate record.">
          <Table>
            <thead>
              <tr><Th>Run</Th><Th>Status</Th><Th>Iterations</Th><Th>Best</Th><Th>Ended</Th></tr>
            </thead>
            <tbody>
              {recent.map((r) => (
                <Row key={String(r.run_id)}>
                  <Td><Mono className="text-ink">{String(r.run_id)}</Mono></Td>
                  <Td>{String(r.status)}</Td>
                  <Td className="tabular">{fmtNum(r.iterations_done)}</Td>
                  <Td className="tabular">
                    {typeof r.best_fitness === "number" ? r.best_fitness.toFixed(4) : "—"}
                  </Td>
                  <Td className="text-ink-faint">{r.ended_at ? fmtTime(r.ended_at) : "—"}</Td>
                </Row>
              ))}
            </tbody>
          </Table>
        </Panel>
      )}
    </div>
  );
};
