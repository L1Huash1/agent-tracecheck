"""Local tool-event recording for the optional LangChain integration.

This module uses TraceRecorder's private _begin/_finish methods internally to
share its exclusive file creation, snapshots, locking and write-failure behavior.
Those methods are implementation details, not a public integration API.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import UUID

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.messages import ToolMessage
except ModuleNotFoundError as error:
    if error.name == "langchain_core":
        raise ImportError(
            'LangChain integration requires the optional dependency: '
            'from the project checkout or extracted source release, run '
            'python -m pip install ".[langchain]". '
            'The project is distributed through GitHub source releases.'
        ) from error
    raise

from ..core import TraceError
from ..recording import TraceRecorder, _snapshot


class LangChainTraceHandler(BaseCallbackHandler):
    """Record LangChain tool callbacks locally without recording tool outputs.

    Attach with ``config={"callbacks": [handler]}`` to real LangChain tools or
    runnables. Only observed tool start/end/error callbacks produce events.
    LangChain run IDs are matched internally to this recorder's ``call-N`` IDs;
    UUIDs and parent relationships are not persisted.

    Arguments use ``inputs`` when supplied, otherwise the original ``input_str``
    string, without guessing JSON. An optional ``argument_filter`` receives a
    deep JSON snapshot and returns the JSON value to record. This protects tool
    inputs from filter mutation, but no automatic redaction is performed.

    All worker calls must finish before ``close``. Missing results are not
    invented. Duplicate or unknown run IDs, invalid arguments and recording
    failures raise; callbacks run inline so these errors are not silently hidden.
    """

    # LangChain's async callback manager runs inline synchronous handlers before
    # non-inline handlers; raise_error makes callback failures visible to callers.
    run_inline = True
    raise_error = True

    def __init__(self, path: str | os.PathLike[str], *,
                 argument_filter: Callable[[Any], Any] | None = None):
        if argument_filter is not None and not callable(argument_filter):
            raise TypeError("argument_filter: expected a callable or None")
        self._recorder = TraceRecorder(path)
        self.path: Path = self._recorder.path
        self._callback_lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._seen: set[str] = set()
        self._argument_filter = argument_filter

    @staticmethod
    def _run_key(run_id: UUID | str) -> str:
        if isinstance(run_id, UUID):
            return str(run_id)
        if isinstance(run_id, str) and run_id:
            return run_id
        raise TraceError("run_id: expected a UUID or a nonempty string")

    def on_tool_start(self, serialized: dict[str, Any] | None, input_str: str, *,
                      run_id: UUID, inputs: dict[str, Any] | None = None,
                      **kwargs: Any) -> None:
        key = self._run_key(run_id)
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if name is None:
            name = kwargs.get("name")
        arguments = _snapshot(inputs if inputs is not None else input_str)
        if self._argument_filter is not None:
            arguments = _snapshot(self._argument_filter(arguments))
        with self._callback_lock:
            if key in self._seen:
                raise TraceError("run_id: duplicate tool start")
            event_id = self._recorder._begin(name, arguments, lambda: None)
            self._active[key] = event_id
            self._seen.add(key)

    def _finish_run(self, run_id: UUID, status: str) -> None:
        key = self._run_key(run_id)
        with self._callback_lock:
            if key not in self._active:
                raise TraceError("run_id: unknown or already completed tool run")
            event_id = self._active[key]
            try:
                self._recorder._finish(event_id, status)
            finally:
                del self._active[key]

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        # A handled ToolException can reach on_tool_end as an error ToolMessage.
        # Plain text (including text that says "error") is never guessed at.
        failed = isinstance(output, ToolMessage) and output.status == "error"
        self._finish_run(run_id, "error" if failed else "ok")

    def on_tool_error(self, error: BaseException, *, run_id: UUID,
                      **kwargs: Any) -> None:
        try:
            self._finish_run(run_id, "error")
        except TraceError:
            raise  # Invalid callback identity is an integration error.
        except BaseException as recording_error:
            # Preserve the actual tool failure, with the recording failure
            # available as its cause, just like TraceRecorder.call/acall.
            raise error from recording_error

    def close(self, *, allow_pending: bool = False) -> None:
        """Close after callbacks finish, or explicitly retain incomplete calls.

        The default refuses unfinished runs and leaves the file open. Once all
        framework tasks have stopped, ``allow_pending=True`` can close a partial
        trace without inventing results. Further callbacks are then rejected.
        This is useful when cancellation prevents an end/error callback.
        """
        if not isinstance(allow_pending, bool):
            raise TypeError("allow_pending: expected a boolean")
        with self._callback_lock:
            if allow_pending:
                # No tool code runs through this recorder: only callbacks use
                # its private writer. Both locks prevent an end callback from
                # racing with the deliberate abandonment of pending markers.
                with self._recorder._lock:
                    self._recorder._inflight.clear()
                self._active.clear()
            self._recorder.close()

    def __enter__(self) -> LangChainTraceHandler:
        with self._callback_lock:
            self._recorder.__enter__()
        return self

    def __exit__(self, exc_type: Any, exception: BaseException | None,
                 traceback: Any) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exception is not None:
                raise exception.with_traceback(traceback) from close_error
            raise
        return False
