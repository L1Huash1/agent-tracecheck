"""Analyze normalized tool events without executing tools or calling a model."""

from __future__ import annotations

from decimal import Decimal
import math
from typing import Any


class TraceError(ValueError):
    """An event, timestamp, or analyzer option is malformed."""


def _validate_json(value: Any, path: str = "events", ancestors=None) -> None:
    """Reject Python-only values, non-finite numbers, and circular structures."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (list, dict):
        raise TraceError(f"{path}: expected a finite JSON value")
    ancestors = set() if ancestors is None else ancestors
    if id(value) in ancestors:
        raise TraceError(f"{path}: circular structures are not JSON")
    ancestors.add(id(value))
    try:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TraceError(f"{path}: JSON object keys must be strings")
            entries = value.items()
        else:
            entries = enumerate(value)
        for key, child in entries:
            _validate_json(child, f"{path}/{key}", ancestors)
    finally:
        ancestors.remove(id(value))


def _canonical(value: Any) -> Any:
    """JSON equality preserves booleans versus numbers and ignores object order."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) in (int, float):
        return ("number", Decimal(str(value)))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_canonical(item) for item in value))
    return ("object", tuple((key, _canonical(child)) for key, child in sorted(value.items())))


def _number(value: Any, label: str) -> float:
    if type(value) not in (int, float):
        raise TraceError(f"{label}: expected a finite nonnegative number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise TraceError(f"{label}: expected a finite nonnegative number") from error
    if not math.isfinite(number) or number < 0:
        raise TraceError(f"{label}: expected a finite nonnegative number")
    # Preserve both the converted value and its canonical JSON decimal text.
    if number != value or Decimal(str(number)) != Decimal(str(value)):
        raise TraceError(f"{label}: number exceeds supported round-trip precision")
    return number


def _integer(value: Any, label: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise TraceError(f"{label}: expected an integer >= {minimum}")


def analyze_trace(events: list, max_calls: int = 50, max_errors: int = 0,
                  max_duration_ms: float | None = None, repeat_threshold: int = 3) -> dict:
    """Check observed calls, retries, and budgets; this does not assess model quality.

    Calls/results match by ID even when events are out of order. Elapsed duration
    is the span of supplied timestamps, which can understate a partially timed
    trace. Error budgets include orphan error results. Repetition counts identical
    tool names and JSON arguments; repetition alone does not establish a loop.
    """
    _integer(max_calls, "max_calls")
    _integer(max_errors, "max_errors")
    _integer(repeat_threshold, "repeat_threshold", 2)
    if max_duration_ms is not None:
        max_duration_ms = _number(max_duration_ms, "max_duration_ms")
    if not isinstance(events, list):
        raise TraceError("events: expected a list of normalized event objects")
    try:
        _validate_json(events)
    except RecursionError as error:
        raise TraceError("events: JSON structure is too deeply nested") from error

    calls, results, timestamps = {}, {}, []
    for index, event in enumerate(events):
        label = f"event {index + 1}"
        if not isinstance(event, dict):
            raise TraceError(f"{label}: expected an object")
        kind, event_id = event.get("type"), event.get("id")
        if kind not in ("tool_call", "tool_result"):
            raise TraceError(f"{label}/type: expected tool_call or tool_result")
        if not isinstance(event_id, str) or not event_id:
            raise TraceError(f"{label}/id: expected a nonempty string")
        timestamp = None
        if "timestamp_ms" in event:
            timestamp = _number(event["timestamp_ms"], label + "/timestamp_ms")
            timestamps.append(timestamp)
        if kind == "tool_call":
            if event_id in calls:
                raise TraceError(f"{label}/id: duplicate tool_call id {event_id!r}")
            tool = event.get("tool")
            if not isinstance(tool, str) or not tool:
                raise TraceError(f"{label}/tool: expected a nonempty string")
            calls[event_id] = {"id": event_id, "tool": tool,
                               "arguments": event.get("arguments", {}),
                               "timestamp": timestamp, "index": index}
        else:
            if event_id in results:
                raise TraceError(f"{label}/id: duplicate tool_result id {event_id!r}")
            if event.get("status") not in ("ok", "error"):
                raise TraceError(f"{label}/status: expected ok or error")
            results[event_id] = {"status": event["status"], "timestamp": timestamp, "index": index}

    findings, analyzed_calls, groups = [], [], {}

    def add(severity, code, message, call_ids=None, tool=None):
        finding = {"severity": severity, "code": code, "message": message}
        if call_ids is not None:
            finding["call_ids"] = call_ids
        if tool is not None:
            finding["tool"] = tool
        findings.append(finding)

    for event_id, result in results.items():
        if event_id not in calls:
            add("error", "orphan_result", "Tool result has no matching call in this trace.", [event_id])

    for event_id, call in calls.items():
        result = results.get(event_id)
        status = result["status"] if result else "pending"
        duration = None
        if result and call["timestamp"] is not None and result["timestamp"] is not None:
            duration = result["timestamp"] - call["timestamp"]
            if duration < 0:
                raise TraceError(f"call {event_id!r}: result timestamp precedes its call timestamp")
        analyzed_calls.append({"id": event_id, "tool": call["tool"], "arguments": call["arguments"],
                               "status": status, "duration_ms": duration})
        if status == "pending":
            add("warning", "missing_result", "Tool call has no matching result in this trace.",
                [event_id], call["tool"])
        elif status == "error":
            add("warning", "tool_error", "Tool call returned an error result.", [event_id], call["tool"])
        try:
            signature = (call["tool"], _canonical(call["arguments"]))
        except RecursionError as error:
            raise TraceError("arguments: JSON structure is too deeply nested") from error
        groups.setdefault(signature, []).append(call)

    for group in groups.values():
        tool = group[0]["tool"]
        if len(group) >= repeat_threshold:
            add("warning", "repeated_call",
                f"Identical tool and arguments observed {len(group)} times; check whether repetition is intended.",
                [call["id"] for call in group], tool)
        failed = [(call, results[call["id"]]) for call in group
                  if call["id"] in results and results[call["id"]]["status"] == "error"]
        # Two earliest candidates suffice: one may be this call's own result.
        # Supplied timestamps take precedence over potentially reordered events.
        first_indices = sorted((r["index"], c["id"]) for c, r in failed)[:2]
        untimed_indices = sorted((r["index"], c["id"]) for c, r in failed
                                 if r["timestamp"] is None)[:2]
        first_times = sorted((r["timestamp"], c["id"]) for c, r in failed
                             if r["timestamp"] is not None)[:2]
        retries = []
        for call in group:
            indices = first_indices if call["timestamp"] is None else untimed_indices
            ordered = any(event_id != call["id"] and index < call["index"]
                          for index, event_id in indices)
            timed = call["timestamp"] is not None and any(
                event_id != call["id"] and timestamp < call["timestamp"]
                for timestamp, event_id in first_times)
            if ordered or timed:
                retries.append(call["id"])
        if retries:
            add("warning", "error_retry",
                "Identical tool and arguments were called after an observed error; check the retry policy.",
                retries, tool)

    duration = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else None
    if 0 < len(timestamps) < len(events):
        add("warning", "partial_timing",
            "Some timestamps are missing. Duration uses only available timestamps and may understate the full trace span.")
    if max_duration_ms is not None and duration is None:
        add("warning", "duration_unavailable",
            "At least two timestamps are needed to assess the duration budget.")
    errors = sum(result["status"] == "error" for result in results.values())
    pending = sum(call["status"] == "pending" for call in analyzed_calls)
    if len(calls) > max_calls:
        add("error", "call_budget", f"Observed {len(calls)} calls exceeds the call budget of {max_calls}.")
    if errors > max_errors:
        add("error", "error_budget", f"Observed {errors} error results exceeds the error budget of {max_errors}.")
    if max_duration_ms is not None and duration is not None and duration > max_duration_ms:
        add("error", "duration_budget",
            f"Observed timestamp span {duration:g} ms exceeds the duration budget of {max_duration_ms:g} ms.")
    return {"schema_version": 1,
            "summary": {"calls": len(calls), "results": len(results), "errors": errors,
                        "pending": pending, "duration_ms": duration, "findings": len(findings)},
            "findings": findings, "calls": analyzed_calls}
