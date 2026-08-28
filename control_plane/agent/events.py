from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Iterable, Iterator

from ..telemetry.events import Event
from ..telemetry.redaction import Redactor, default_redactor


class EventLog:
    def __init__(self, path: str, redactor: Redactor | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._redactor = redactor or default_redactor()

    def append(self, event: Event) -> None:
        self._redactor.redact_event(event)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
            stream.flush()

    def append_many(self, events: Iterable[Event]) -> int:
        count = 0
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            for event in events:
                self._redactor.redact_event(event)
                stream.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
                count += 1
            stream.flush()
        return count

    def replay(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield Event.from_dict(json.loads(line))
