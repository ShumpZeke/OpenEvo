"""
"Where was I?" — computed, never stored.

The digest below answers the question someone actually has when they come back
to this project after a week: what did I run, how did it go, what can I pick
up, and what did I leave a note about.

**Every number here is derived at read time** from the projections the event
log already produced. Nothing is cached and nothing is duplicated into a
summary table, for the reason the storage layer is built around: a second copy
drifts from the first, and a drifted summary is worse than no summary because
it is believed. If a run is deleted or the projections are rebuilt, this
changes with them automatically.

The one thing it does read that is *not* derived is the journal, which exists
precisely for what cannot be reconstructed — see `journal.py`.

No fabricated data: a field with nothing behind it is `None`, and the renderers
show "no data" rather than a zero. An empty workspace produces an empty digest,
not a plausible-looking one.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# How far back "recent" reaches when nothing else bounds the query.
DEFAULT_WINDOW_DAYS = 30.0


@dataclass
class ResumePoint:
    """One thing you could pick up again, and what it would cost to do so."""

    run_id: str
    output_dir: Optional[str]
    checkpoint_path: Optional[str]
    checkpoint_iteration: Optional[int]
    best_fitness: Optional[float]
    iterations_done: Optional[int]
    iterations_target: Optional[int]
    status: str
    ended_at: Optional[float]
    # The exact command, so resuming is copy-paste rather than reconstruction.
    resume_command: Optional[str] = None
    task: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _output_dir_for(run: Dict[str, Any], checkpoint_path: Optional[str]) -> Optional[str]:
    """
    Where the run wrote, recovering it from the checkpoint when unrecorded.

    `runs.output_dir` was NULL for every run until the started-event handler
    was fixed to store it, so history recorded before that fix has only the
    checkpoint path — `<output>/checkpoints/checkpoint_N`, two levels down.
    Deriving it here means old runs are resumable too, rather than only ones
    created after the fix.
    """
    recorded = run.get("output_dir")
    if recorded:
        return recorded
    source = checkpoint_path or run.get("checkpoint_dir")
    if not source:
        return None
    parent = os.path.dirname(os.path.dirname(source.rstrip("/\\")))
    return parent or None


def _infer_task(output_dir: Optional[str]) -> Optional[str]:
    """
    Recover the task from the run directory name.

    `run-evolution` names directories `runs/<timestamp>-<task>-<profile>`. This
    is the same inference `resume-evolution` makes, and it is inference: a
    hand-named directory yields None rather than a guess, because resuming with
    the wrong evaluator produces confident scores for a different problem.
    """
    if not output_dir:
        return None
    base = os.path.basename(output_dir.rstrip("/\\"))
    parts = base.split("-")
    # <8-digit date>-<6-digit time>-<task...>-<profile>
    if len(parts) >= 4 and len(parts[0]) == 8 and parts[0].isdigit():
        if parts[-1] in ("max", "stock"):
            candidate = "-".join(parts[2:-1])
            return candidate or None
    return None


def _resume_command(point: "ResumePoint") -> Optional[str]:
    if not point.output_dir or not point.checkpoint_path:
        return None
    cmd = f"./scripts/resume-evolution.sh {point.output_dir}"
    if point.task:
        cmd += f" --task {point.task}"
    return cmd


def _runs(store: Any, *, limit: int, since: Optional[float]) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM runs"
    params: List[Any] = []
    if since is not None:
        # A run with no timestamp is INCLUDED, not treated as epoch-old.
        #
        # Coalescing a missing timestamp to 0 put every such run before any
        # window and made it vanish from the digest — a run that exists,
        # holds a checkpoint, and is resumable, silently absent from the one
        # view whose job is to tell you what you can resume. A run in the
        # database is a run that happened; not knowing when is a reason to
        # show it, not to hide it.
        sql += (" WHERE started_at IS NULL AND ended_at IS NULL"
                " OR COALESCE(ended_at, started_at) >= ?")
        params.append(since)
    sql += " ORDER BY COALESCE(started_at, ended_at, 0) DESC LIMIT ?"
    params.append(limit)
    return store.query(sql, params)


def _latest_checkpoint(store: Any, run_id: str) -> Optional[Dict[str, Any]]:
    return store.query_one(
        "SELECT * FROM checkpoints WHERE run_id = ? AND status = 'ok'"
        " ORDER BY iteration DESC LIMIT 1",
        (run_id,),
    )


def build_digest(
    store: Any,
    *,
    limit: int = 10,
    window_days: Optional[float] = DEFAULT_WINDOW_DAYS,
    journal_limit: int = 10,
) -> Dict[str, Any]:
    """
    Everything needed to pick the project back up, in one object.

    `window_days=None` means "all of it" — useful when you have been away long
    enough that the default window would hide the very thing you are looking
    for, which is the case this whole function exists to serve.
    """
    since = None if window_days is None else time.time() - window_days * 86400.0
    runs = _runs(store, limit=max(1, min(int(limit), 100)), since=since)

    resumable: List[ResumePoint] = []
    for run in runs:
        ckpt = _latest_checkpoint(store, run["run_id"])
        output_dir = _output_dir_for(run, (ckpt or {}).get("path"))
        point = ResumePoint(
            run_id=run["run_id"],
            output_dir=output_dir,
            checkpoint_path=(ckpt or {}).get("path"),
            checkpoint_iteration=(ckpt or {}).get("iteration"),
            best_fitness=run.get("best_fitness"),
            iterations_done=run.get("iterations_done"),
            iterations_target=run.get("iterations_target"),
            status=run.get("status") or "unknown",
            ended_at=run.get("ended_at"),
            task=_infer_task(output_dir),
        )
        point.resume_command = _resume_command(point)
        if point.checkpoint_path:
            resumable.append(point)

    # The best result ever recorded, not merely the best in the window. Someone
    # returning after a month wants to know the high-water mark, and hiding it
    # behind a date filter would answer a question they did not ask.
    best = store.query_one(
        "SELECT run_id, best_fitness, output_dir FROM runs"
        " WHERE best_fitness IS NOT NULL"
        " ORDER BY best_fitness DESC LIMIT 1"
    )

    unfinished = [
        r for r in runs
        if (r.get("status") or "") in ("running", "starting", "created")
    ]

    from .journal import Journal

    notes = Journal(store).list(limit=max(1, min(int(journal_limit), 100)))

    totals = store.query_one(
        "SELECT COUNT(*) AS runs,"
        " SUM(COALESCE(iterations_done, 0)) AS iterations FROM runs"
    ) or {}

    return {
        "generated_at": time.time(),
        "window_days": window_days,
        "totals": {
            "runs": totals.get("runs") or 0,
            # None rather than 0 when nothing has run: "no data" and "zero
            # iterations across four runs" are different facts.
            "iterations": totals.get("iterations"),
        },
        "recent_runs": runs,
        "resumable": [p.to_dict() for p in resumable],
        "best_ever": best,
        # A run still marked running after the process died is reconciled on
        # startup; one listed here during a live session is genuinely in flight.
        "unfinished": unfinished,
        "journal": [n.to_dict() for n in notes],
    }


def render_text(digest: Dict[str, Any]) -> str:
    """
    The digest as a terminal briefing.

    Shared by the CLI and the API so the two cannot describe the same state
    differently — which they would, eventually, if each formatted its own.
    """
    out: List[str] = []
    totals = digest.get("totals") or {}
    runs_n = totals.get("runs") or 0

    out.append("=" * 72)
    out.append("  EVOLUTION — where you left off")
    out.append("=" * 72)

    if not runs_n:
        out.append("")
        out.append("  No runs recorded yet in this workspace.")
        out.append("  Start one:  ./scripts/run-evolution.sh --iterations 10")
        out.append("")
        return "\n".join(out)

    iters = totals.get("iterations")
    out.append(f"  {runs_n} run(s) recorded"
               + (f", {iters} iterations total" if iters is not None else ""))

    best = digest.get("best_ever")
    if best and best.get("best_fitness") is not None:
        out.append(f"  best score ever: {best['best_fitness']:.4f}"
                   f"  ({best.get('output_dir') or best['run_id']})")
    else:
        out.append("  best score ever: no scored run yet")

    unfinished = digest.get("unfinished") or []
    if unfinished:
        out.append("")
        out.append(f"  {len(unfinished)} run(s) still marked in progress:")
        for r in unfinished[:5]:
            out.append(f"    {r['run_id']}  status={r.get('status')}")

    resumable = digest.get("resumable") or []
    out.append("")
    if resumable:
        out.append(f"  RESUMABLE ({len(resumable)}):")
        for p in resumable[:5]:
            score = (f"{p['best_fitness']:.4f}"
                     if p.get("best_fitness") is not None else "no score")
            out.append(f"    {p.get('output_dir') or p['run_id']}")
            out.append(f"      checkpoint {p.get('checkpoint_iteration')}"
                       f"  best {score}  status {p.get('status')}")
            if p.get("resume_command"):
                out.append(f"      $ {p['resume_command']}")
    else:
        out.append("  RESUMABLE: none — no run has written a checkpoint yet.")
        out.append("    Checkpoints are written every `checkpoint_interval`")
        out.append("    iterations (6 by default), so short runs have none.")

    notes = digest.get("journal") or []
    out.append("")
    if notes:
        out.append(f"  JOURNAL (latest {len(notes)}):")
        for n in notes[:8]:
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(n.get("created_at") or 0))
            mark = "*" if n.get("source") == "user" else " "
            out.append(f"   {mark} [{n.get('kind')}] {when}  {n.get('title')}")
            if n.get("detail"):
                first = str(n["detail"]).strip().splitlines()[0]
                out.append(f"        {first[:66]}")
        out.append("     (* = written by you; unmarked = recorded by an agent)")
    else:
        out.append("  JOURNAL: empty.")
        out.append("    Leave yourself a note:")
        out.append("    ./scripts/memory.sh note \"what I was doing\"")

    out.append("")
    return "\n".join(out)
