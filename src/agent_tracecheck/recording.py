"""Record local Python tool calls as normalized JSONL events.

Arguments may contain sensitive data. Callers must redact them before calling
``call``; this recorder does not perform automatic redaction. Return values and
exception messages, types, or tracebacks are never written to the trace.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Awaitable, Callable, TypeVar

from .core import TraceError, _validate_json


_Result = TypeVar("_Result")


def _snapshot(arguments: Any) -> Any:
    try:
        _validate_json(arguments, "arguments")
        # The JSON round trip also snapshots every nested list and dictionary.
        return json.loads(json.dumps(arguments, ensure_ascii=True, allow_nan=False))
    except RecursionError as error:
        raise TraceError("arguments: JSON structure is too deeply nested") from error
    except (TypeError, ValueError) as error:
        if isinstance(error, TraceError):
            raise
        raise TraceError("arguments: expected strictly serializable finite JSON") from error


class TraceRecorder:
    """Record synchronous or asynchronous Python tools without saving results.

    ``TraceRecorder(path)`` exclusively creates a UTF-8 JSONL file and its parent
    directories. Existing files are never overwritten. Calls get unique IDs and
    relative monotonic ``timestamp_ms`` values. Writes are flushed under a lock;
    tool code runs outside that lock so parallel and nested calls are supported.
    Use ``call`` for synchronous tools and ``await acall`` for asynchronous tools.

    ``arguments`` must contain only finite JSON values with string object keys.
    They are snapshotted before the callable runs. Redact sensitive arguments
    yourself; no automatic redaction is performed. Do not mutate the arguments
    concurrently while their snapshot is being taken.

    Join worker calls before leaving the context. ``close()`` raises a clear
    ``RuntimeError`` while calls are in flight and leaves the file open so those
    calls can finish; call ``close()`` again afterward. Recording failures are
    propagated. Tool exceptions are re-raised, including when recording fails.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._start_ns = time.monotonic_ns()
        self._counter = 0
        self._inflight: set[str] = set()
        self._closed = False
        self._failure: BaseException | None = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TraceRecorder is closed")
        if self._failure is not None:
            raise RuntimeError("TraceRecorder cannot continue after a recording failure") from self._failure

    def _write(self, event: dict[str, Any]) -> None:
        try:
            self._stream.write(json.dumps(event, ensure_ascii=True, allow_nan=False,
                                          separators=(",", ":")) + "\n")
            self._stream.flush()
        except BaseException as error:
            self._failure = error
            raise

    def _timestamp(self) -> float:
        return (time.monotonic_ns() - self._start_ns) / 1_000_000

    def _finish(self, event_id: str, status: str) -> None:
        with self._lock:
            try:
                self._ensure_open()
                self._write({"type": "tool_result", "id": event_id,
                             "timestamp_ms": self._timestamp(), "status": status})
            finally:
                self._inflight.remove(event_id)

    def _begin(self, tool: str, arguments: Any, function: Callable[[], Any]) -> str:
        if not isinstance(tool, str) or not tool.strip():
            raise TraceError("tool: expected a nonempty, non-whitespace string")
        if not callable(function):
            raise TypeError("function: expected a callable accepting no arguments")
        arguments = _snapshot(arguments)
        with self._lock:
            self._ensure_open()
            self._counter += 1
            event_id = f"call-{self._counter}"
            self._write({"type": "tool_call", "id": event_id, "tool": tool,
                         "arguments": arguments, "timestamp_ms": self._timestamp()})
            self._inflight.add(event_id)
        return event_id

    def _record_error(self, event_id: str, tool_error: BaseException) -> None:
        try:
            self._finish(event_id, "error")
        except BaseException as recording_error:
            raise tool_error from recording_error

    def call(self, tool: str, arguments: Any, function: Callable[[], _Result]) -> _Result:
        """Record a synchronous invocation and return its original result.

        The callable receives no arguments: use a lambda or ``functools.partial``
        to wrap an existing tool. Only its ``ok`` or ``error`` status is saved.
        Sensitive arguments must already be redacted before this method runs.
        Invalid inputs and failed call-event writes never execute the callable.

        Returning an awaitable records an error and raises ``TypeError``: use
        ``acall`` instead. Unstarted native coroutines are closed to prevent
        unawaited-coroutine warnings; external Futures are never cancelled.
        """
        event_id = self._begin(tool, arguments, function)
        try:
            result = function()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result) and inspect.getcoroutinestate(result) == inspect.CORO_CREATED:
                    result.close()
                raise TypeError("Tool returned an awaitable; use TraceRecorder.acall(...) instead of call(...)")
        except BaseException as tool_error:
            self._record_error(event_id, tool_error)
            raise
        self._finish(event_id, "ok")
        return result

    async def acall(self, tool: str, arguments: Any,
                    function: Callable[[], Awaitable[_Result]]) -> _Result:
        """Record ``await function()`` with the same data boundaries as ``call``.

        Concurrent asyncio tasks receive independent IDs. Cancellation records
        ``error`` and propagates the original ``CancelledError`` unchanged.
        The callable must return an awaitable; it runs outside the write lock.
        Arguments may be sensitive and must be redacted by the caller.
        """
        event_id = self._begin(tool, arguments, function)
        try:
            awaitable = function()
            if not inspect.isawaitable(awaitable):
                raise TypeError("TraceRecorder.acall(...) requires a callable returning an awaitable")
            result = await awaitable
        except BaseException as tool_error:
            self._record_error(event_id, tool_error)
            raise
        self._finish(event_id, "ok")
        return result

    def close(self) -> None:
        """Close the file; refuse while calls are in flight. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            if self._inflight:
                raise RuntimeError(f"Cannot close TraceRecorder: {len(self._inflight)} call(s) are still in flight; join them first")
            try:
                self._stream.close()
            finally:
                self._closed = True

    def __enter__(self) -> TraceRecorder:
        with self._lock:
            self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exception: BaseException | None, traceback: Any) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exception is not None:
                raise exception.with_traceback(traceback) from close_error
            raise
        return False
