# Normalized tool trace, version 1

Use UTF-8 JSONL: one complete JSON object on each nonblank line. Blank lines and an initial UTF-8 BOM are allowed. JSON comments, NaN/Infinity, and duplicate JSON object keys are rejected by the CLI. An empty trace is valid.

## Calls

```json
{"type":"tool_call","id":"c1","tool":"lookup","arguments":{"query":"demo"},"timestamp_ms":0}
```

`id` and `tool` must be nonempty strings. `arguments` is any finite JSON value and defaults to `{}` when omitted. `timestamp_ms` is an optional finite, nonnegative number; a boolean is not a number for this field.

## Results

```json
{"type":"tool_result","id":"c1","status":"error","timestamp_ms":20}
```

`id` must be nonempty and `status` must be exactly `ok` or `error`. Output bodies are not required. Timestamps follow the same rules as calls. Additional JSON fields are accepted but ignored by diagnostics, so include only data you need.

Call IDs must be unique among calls, and result IDs among results. A result may precede its call in file order; matching uses IDs. A result with no call becomes an error finding. A call with no result becomes a pending-call warning. If a matched result timestamp is earlier than its call timestamp, the trace is invalid.

The CLI rejects decimal tokens that cannot round-trip through its supported numeric representation instead of silently merging distinct arguments. For example, `0.1` works; `0.100000000000000000001` is rejected. JSONL records are separated by LF (CRLF is also accepted); Unicode separators inside JSON strings remain string content.

## Timing and order

Use one consistent monotonic or epoch clock and unit throughout a trace. Observed span is `max(timestamp_ms) - min(timestamp_ms)` when at least two timestamps exist; otherwise it is unavailable. This can include orphan results. Partial timestamp coverage produces a warning and may underestimate real elapsed time. The duration budget applies to this observed span only; it cannot enforce duration for unrecorded intervals.

Timestamps must also convert to the analyzer's finite numeric representation without changing their value. Unsupported very large integer timestamps are rejected rather than silently merged. Use practical millisecond values from a consistent clock; integer epoch milliseconds and the recorder's relative timestamps fit normal use.

Matched call duration is result timestamp minus call timestamp when both exist, otherwise null. Concurrent call durations are not added together. Retry inference requires evidence that a same-signature failed result preceded a later call. When both relevant timestamps exist, they take precedence; otherwise file event order is used. Call order alone is insufficient for concurrency.

## Signatures and findings

A repeat signature consists of the tool name and canonicalized arguments. Object key order is ignored; array order matters; booleans differ from numbers; `1` and `1.0` are equal numeric values. Repetition at or above `repeat_threshold` is a warning, not a diagnosis of an infinite loop.

Budget findings have severity `error`. Tool errors, repetition, pending calls, and partial timing use `warning`; orphan results use `error`. Every error result counts toward `max_errors`, including orphan errors. The default CLI fails on error findings, not on warnings alone.

JSON reports use `schema_version: 1`, summary counts, a findings list, and a call list in recorded call order. Original arguments are included in the call list. No model reasoning, model weights, tokens, billing information, or tool output content is required or inferred.
