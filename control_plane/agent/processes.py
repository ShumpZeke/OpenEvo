from __future__ import annotations

import codecs
import os
import subprocess
import uuid
from collections import deque
from pathlib import Path
from threading import Lock, Thread
from types import TracebackType
from typing import BinaryIO, Final

import psutil

from .process_types import (
    ProcessInputClosedError,
    ProcessOutputChunk,
    ProcessSessionId,
    ProcessSnapshot,
    ProcessSpec,
    ProcessState,
    ProcessStream,
    ProcessTerminationError,
    ProcessWorkspaceEscapeError,
    UnknownProcessSessionError,
)

_READ_SIZE: Final = 4096
_MAX_OUTPUT_BYTES: Final = 1_048_576
_READER_JOIN_SECONDS: Final = 2.0


class _ProcessSession:
    def __init__(
        self,
        session_id: ProcessSessionId,
        process: subprocess.Popen[bytes],
    ) -> None:
        self.session_id = session_id
        self.process = process
        self.lock = Lock()
        self.stdin_lock = Lock()
        self.chunks: deque[ProcessOutputChunk] = deque()
        self.output_bytes = 0
        self.next_sequence = 1
        self.terminated = False
        self.readers: tuple[Thread, ...] = ()

    def append(self, stream: ProcessStream, text: str) -> None:
        if not text:
            return
        size = len(text.encode("utf-8", errors="replace"))
        with self.lock:
            self.chunks.append(ProcessOutputChunk(self.next_sequence, stream, text))
            self.next_sequence += 1
            self.output_bytes += size
            while self.output_bytes > _MAX_OUTPUT_BYTES and len(self.chunks) > 1:
                removed = self.chunks.popleft()
                self.output_bytes -= len(removed.text.encode("utf-8", errors="replace"))


class ProcessSessionManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._sessions: dict[ProcessSessionId, _ProcessSession] = {}
        self._lock = Lock()

    def __enter__(self) -> ProcessSessionManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.shutdown()

    def start(self, spec: ProcessSpec) -> ProcessSnapshot:
        cwd = self._resolve_cwd(spec.cwd)
        environment = os.environ.copy()
        environment.update(dict(spec.environment))
        command = (spec.program, *spec.arguments)
        if os.name == "nt":
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        session_id = ProcessSessionId(f"process_{uuid.uuid4().hex}")
        session = _ProcessSession(session_id, process)
        with self._lock:
            self._sessions[session_id] = session
        session.readers = self._start_readers(session)
        return self.read(session_id)

    def read(self, session_id: ProcessSessionId, cursor: int = 0) -> ProcessSnapshot:
        return self._snapshot(self._session(session_id), cursor)

    def write(self, session_id: ProcessSessionId, data: str) -> ProcessSnapshot:
        session = self._session(session_id)
        stream = session.process.stdin
        if stream is None or stream.closed:
            raise ProcessInputClosedError(session_id)
        with session.stdin_lock:
            try:
                stream.write(data.encode("utf-8"))
                stream.flush()
            except (BrokenPipeError, OSError) as error:
                raise ProcessInputClosedError(session_id) from error
        return self._snapshot(session, 0)

    def wait(
        self,
        session_id: ProcessSessionId,
        timeout_s: float,
    ) -> ProcessSnapshot:
        session = self._session(session_id)
        try:
            session.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return self._snapshot(session, 0, timed_out=True)
        self._join_readers(session)
        return self._snapshot(session, 0)

    def terminate(
        self,
        session_id: ProcessSessionId,
        grace_s: float = 2.0,
    ) -> ProcessSnapshot:
        session = self._session(session_id)
        is_running = session.process.poll() is None
        if is_running:
            self._terminate_tree(session, grace_s)
            session.terminated = True
        self._join_readers(session)
        return self._snapshot(session, 0)

    def shutdown(self) -> None:
        with self._lock:
            session_ids = tuple(self._sessions)
        for session_id in session_ids:
            session = self._session(session_id)
            if session.process.poll() is None:
                self.terminate(session_id)

    def _resolve_cwd(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ProcessWorkspaceEscapeError(relative)
        return candidate

    def _session(self, session_id: ProcessSessionId) -> _ProcessSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise UnknownProcessSessionError(session_id)
        return session

    @staticmethod
    def _start_readers(session: _ProcessSession) -> tuple[Thread, ...]:
        streams = (
            (ProcessStream.STDOUT, session.process.stdout),
            (ProcessStream.STDERR, session.process.stderr),
        )
        threads: list[Thread] = []
        for stream_name, stream in streams:
            if stream is None:
                continue
            thread = Thread(
                target=ProcessSessionManager._drain,
                args=(session, stream_name, stream),
                name=f"{session.session_id}-{stream_name.value}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        return tuple(threads)

    @staticmethod
    def _drain(
        session: _ProcessSession,
        stream_name: ProcessStream,
        stream: BinaryIO,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            block = stream.read(_READ_SIZE)
            if not block:
                break
            session.append(stream_name, decoder.decode(block))
        session.append(stream_name, decoder.decode(b"", final=True))
        stream.close()

    @staticmethod
    def _join_readers(session: _ProcessSession) -> None:
        for reader in session.readers:
            reader.join(timeout=_READER_JOIN_SECONDS)

    @staticmethod
    def _terminate_tree(session: _ProcessSession, grace_s: float) -> None:
        try:
            parent = psutil.Process(session.process.pid)
            targets = (*parent.children(recursive=True), parent)
            for target in targets:
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    continue
            _, alive = psutil.wait_procs(targets, timeout=grace_s)
            for target in alive:
                target.kill()
            if alive:
                psutil.wait_procs(alive, timeout=grace_s)
            session.process.wait(timeout=max(grace_s, 0.1))
        except psutil.NoSuchProcess:
            session.process.poll()
        except (psutil.AccessDenied, subprocess.TimeoutExpired) as error:
            session.process.kill()
            session.process.wait(timeout=5.0)
            raise ProcessTerminationError(
                session.session_id, session.process.pid
            ) from error

    @staticmethod
    def _snapshot(
        session: _ProcessSession,
        cursor: int,
        timed_out: bool = False,
    ) -> ProcessSnapshot:
        returncode = session.process.poll()
        if session.terminated:
            state = ProcessState.TERMINATED
        elif returncode is None:
            state = ProcessState.RUNNING
        else:
            state = ProcessState.EXITED
        with session.lock:
            chunks = tuple(chunk for chunk in session.chunks if chunk.sequence > cursor)
            oldest = (
                session.chunks[0].sequence if session.chunks else session.next_sequence
            )
            latest = session.next_sequence - 1
        return ProcessSnapshot(
            session.session_id,
            session.process.pid,
            state,
            returncode,
            latest,
            chunks,
            truncated=cursor < oldest - 1,
            timed_out=timed_out,
        )
