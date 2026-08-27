"""
`evolution-memory` — the way back into the project.

Three verbs, chosen because they are the three things someone actually does
after time away: see where they were, write down where they are, and find
something they half-remember.

    evolution-memory                       # the digest: runs, resumable, notes
    evolution-memory note "..." [--kind]   # leave yourself something
    evolution-memory search "..."          # find it again

It reads the same workspace the Control Center does, so the terminal and the
browser cannot disagree about what happened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from ..storage.store import Store
from .importer import import_all
from .journal import KINDS, Journal
from .resume import build_digest, render_text

DEFAULT_WORKSPACE = os.path.join(".evolution", "workspace")


def _store(workspace: Optional[str]) -> Store:
    root = os.path.abspath(
        workspace or os.environ.get("EVOLUTION_WORKSPACE", DEFAULT_WORKSPACE))
    path = os.path.join(root, "control_plane.db")
    if not os.path.exists(path):
        # An explicit, actionable message. "unable to open database file" is
        # what SQLite would say, and it sends people looking for a permissions
        # problem they do not have.
        print(f"No workspace database at {path}.", file=sys.stderr)
        print("Run ./bootstrap.sh first, or set EVOLUTION_WORKSPACE.",
              file=sys.stderr)
        raise SystemExit(1)
    return Store(path)


def _cmd_show(args: argparse.Namespace) -> int:
    store = _store(args.workspace)
    try:
        # Pull in runs launched from the shell before reporting.
        #
        # The collector only ingests while the Control Center is up, so a run
        # started with `./scripts/run-evolution.sh` alone left its events in a
        # file and nothing else. Importing here is what makes "my history" mean
        # the same thing however you launched. It is idempotent and offset-
        # tracked, so on the common path it reads nothing.
        if not args.no_import:
            summary = import_all(store)
            if summary.get("events") and not args.json:
                print(f"(imported {summary['events']} new events from "
                      f"{summary['files_updated']} run log(s))\n")

        digest = build_digest(
            store,
            limit=args.limit,
            window_days=None if args.all else args.days,
            journal_limit=args.notes,
        )
        if args.json:
            print(json.dumps(digest, indent=2, default=str))
        else:
            print(render_text(digest))
        return 0
    finally:
        store.close()


def _cmd_note(args: argparse.Namespace) -> int:
    store = _store(args.workspace)
    try:
        entry = Journal(store).add(
            " ".join(args.title),
            kind=args.kind,
            detail=args.detail or "",
            run_id=args.run,
            tags=args.tag or [],
            source="user",
        )
        print(f"recorded [{entry.kind}] {entry.title}  ({entry.entry_id})")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


def _cmd_search(args: argparse.Namespace) -> int:
    store = _store(args.workspace)
    try:
        hits = Journal(store).search(" ".join(args.text), limit=args.limit)
        if not hits:
            print("no matching journal entries.")
            return 0
        for e in hits:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created_at))
            print(f"[{e.kind}] {when}  {e.title}")
            if e.detail:
                for line in e.detail.strip().splitlines():
                    print(f"    {line}")
            if e.run_id:
                print(f"    run: {e.run_id}")
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="evolution-memory",
        description="Project memory: where you left off, and what you noted.")
    ap.add_argument("--workspace", help="workspace directory (default: "
                                        "$EVOLUTION_WORKSPACE or .evolution/workspace)")
    sub = ap.add_subparsers(dest="command")

    show = sub.add_parser("show", help="the digest (default command)")
    show.add_argument("--limit", type=int, default=10, help="runs to consider")
    show.add_argument("--days", type=float, default=30.0, help="how far back")
    show.add_argument("--all", action="store_true",
                      help="ignore the date window entirely")
    show.add_argument("--notes", type=int, default=10, help="journal entries to show")
    show.add_argument("--json", action="store_true", help="machine-readable")
    show.add_argument("--no-import", action="store_true",
                      help="skip importing shell-launched run logs")
    show.set_defaults(func=_cmd_show)

    note = sub.add_parser("note", help="record something worth remembering")
    note.add_argument("title", nargs="+")
    note.add_argument("--kind", default="note", choices=list(KINDS))
    note.add_argument("--detail", default="")
    note.add_argument("--run", help="anchor the note to a run id")
    note.add_argument("--tag", action="append")
    note.set_defaults(func=_cmd_note)

    search = sub.add_parser("search", help="find a note again")
    search.add_argument("text", nargs="+")
    search.add_argument("--limit", type=int, default=50)
    search.set_defaults(func=_cmd_search)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Bare `evolution-memory` is the digest. That is the common case by a wide
    # margin, and making people type a subcommand for it would be friction on
    # the one path this tool exists to make easy.
    if not argv or argv[0].startswith("-"):
        known_flags = {"--workspace", "--limit", "--days", "--all", "--notes",
                       "--json", "--no-import", "-h", "--help"}
        if not argv or argv[0] in known_flags or argv[0].startswith("--workspace="):
            argv = ["show"] + argv

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        args = ap.parse_args(["show"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
