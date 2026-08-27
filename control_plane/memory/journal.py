"""
The journal: what the event log cannot reconstruct.

Everything this project records is otherwise derived. Runs, scores,
checkpoints, provider probes and candidate lineage all come out of the event
log, and `Store.rebuild_projections_from_log` can rebuild every one of them
from scratch — which is what makes the projections a cache rather than a second
source of truth.

The journal is the one table that is not like that, and the reason is simple:
**why** something was done was never an event. A decision to stop pinning a
route, a note that a provider looked flaky before it was measured, a reminder
of what someone was in the middle of — none of that is recoverable from a
stream of candidate and request events, however complete.

So the rule for what belongs here is narrow and worth keeping narrow: if it can
be derived, derive it (see `resume.py`, which does exactly that and stores
nothing). Only what cannot be derived is written down.

`source` separates what a person asserted from what a program inferred.
Collapsing those would make the journal untrustworthy for precisely the
decisions it exists to hold.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Kinds are a closed set, so the UI can style them and a query can filter on
# them. An unknown kind is coerced to `note` rather than rejected: losing a
# thought because it was mislabelled would be a worse failure than filing it
# imprecisely.
KINDS = ("note", "decision", "blocker", "milestone", "session")
DEFAULT_KIND = "note"

SOURCES = ("user", "agent")
DEFAULT_SOURCE = "user"


@dataclass
class JournalEntry:
    entry_id: str
    created_at: float
    kind: str
    title: str
    detail: str = ""
    run_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    source: str = DEFAULT_SOURCE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "run_id": self.run_id,
            "tags": list(self.tags),
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "JournalEntry":
        def _load(raw: Any, fallback: Any) -> Any:
            try:
                return json.loads(raw) if raw else fallback
            except (TypeError, ValueError):
                # A hand-edited row must not take down the whole listing. One
                # unreadable field costs that field, not the entry.
                return fallback

        return cls(
            entry_id=row["entry_id"],
            created_at=row["created_at"],
            kind=row["kind"],
            title=row["title"],
            detail=row.get("detail") or "",
            run_id=row.get("run_id"),
            tags=_load(row.get("tags"), []),
            source=row.get("source") or DEFAULT_SOURCE,
            metadata=_load(row.get("metadata"), {}),
        )


class Journal:
    """Append-and-read memory over the store's `journal` table."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def add(
        self,
        title: str,
        *,
        kind: str = DEFAULT_KIND,
        detail: str = "",
        run_id: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        source: str = DEFAULT_SOURCE,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[float] = None,
    ) -> JournalEntry:
        """
        Record one entry.

        A blank title is rejected. An untitled entry is invisible in every
        listing that exists, so accepting one would be a silent discard
        wearing the costume of a successful write.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("a journal entry needs a title")

        entry = JournalEntry(
            entry_id=f"jrn_{uuid.uuid4().hex[:12]}",
            created_at=created_at if created_at is not None else time.time(),
            kind=kind if kind in KINDS else DEFAULT_KIND,
            title=title,
            detail=detail or "",
            run_id=run_id,
            tags=[t for t in (tags or []) if t],
            source=source if source in SOURCES else DEFAULT_SOURCE,
            metadata=dict(metadata or {}),
        )
        self.store.execute(
            "INSERT INTO journal(entry_id, created_at, kind, title, detail,"
            " run_id, tags, source, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (entry.entry_id, entry.created_at, entry.kind, entry.title,
             entry.detail, entry.run_id, json.dumps(entry.tags), entry.source,
             json.dumps(entry.metadata)),
        )
        return entry

    def list(
        self,
        *,
        limit: int = 50,
        kind: Optional[str] = None,
        run_id: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[JournalEntry]:
        """Newest first — the order you want when asking "where was I?"."""
        sql = "SELECT * FROM journal WHERE 1=1"
        params: List[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [JournalEntry.from_row(r) for r in self.store.query(sql, params)]

    def search(self, text: str, *, limit: int = 50) -> List[JournalEntry]:
        """
        Substring match over title and detail.

        Deliberately LIKE and not FTS. The journal is a human-scale table —
        hundreds of rows, not millions — and an FTS index is a second thing to
        keep in step with the first for no gain at this size.
        """
        needle = f"%{(text or '').strip()}%"
        if needle == "%%":
            return self.list(limit=limit)
        return [
            JournalEntry.from_row(r)
            for r in self.store.query(
                "SELECT * FROM journal WHERE title LIKE ? OR detail LIKE ?"
                " ORDER BY created_at DESC LIMIT ?",
                (needle, needle, max(1, min(int(limit), 500))),
            )
        ]

    def get(self, entry_id: str) -> Optional[JournalEntry]:
        row = self.store.query_one(
            "SELECT * FROM journal WHERE entry_id = ?", (entry_id,))
        return JournalEntry.from_row(row) if row else None

    def delete(self, entry_id: str) -> bool:
        """Remove one entry. Returns whether it existed."""
        if self.get(entry_id) is None:
            return False
        self.store.execute("DELETE FROM journal WHERE entry_id = ?", (entry_id,))
        return True

    def counts_by_kind(self) -> Dict[str, int]:
        return {
            r["kind"]: r["n"]
            for r in self.store.query(
                "SELECT kind, COUNT(*) AS n FROM journal GROUP BY kind")
        }
