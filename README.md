# agent-tracecheck

**Find repeated tool calls, incomplete runs, and budget overruns in agent traces.**

[中文说明](README.zh-CN.md) · [Trace format](docs/trace-format.md)

[![Tests](https://github.com/L1Huash1/agent-tracecheck/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/L1Huash1/agent-tracecheck/actions/workflows/ci.yml)

A small, offline diagnostic CLI for a normalized tool-call trace. Python 3.10+, zero runtime dependencies, no model calls and no trace uploads.

It reads recorded events; it never replays or executes a tool. Repetition is a signal to inspect, not proof that an agent is stuck.

## Quick start

```sh
git clone https://github.com/L1Huash1/agent-tracecheck.git
cd agent-tracecheck
python -m pip install .
agent-tracecheck examples/healthy.jsonl
agent-tracecheck examples/retry-loop.jsonl
```

Both examples are **synthetic**. The healthy trace has concurrent calls and passes. The retry example has three failures and an unfinished call, so it intentionally exits **1** under the default zero-error budget.

```sh
# Machine-readable report without failing on diagnostic findings
agent-tracecheck examples/retry-loop.jsonl --format json --fail-on never

# Enforce explicit budgets and warnings in CI
agent-tracecheck trace.jsonl --max-calls 20 --max-errors 1 --max-duration-ms 30000 --fail-on warning

# Save a report (the threshold exit status still applies)
agent-tracecheck examples/retry-loop.jsonl --format markdown --output report.md
```

## What it checks

- Identical tool names and arguments repeated at or above the selected threshold.
- Failed tool results and evidence of retries after an earlier failure.
- Calls with no result, and results with no corresponding call.
- Total call count, error-result count, and observed timestamp-span budgets.
- Partial timing information, invalid event shapes, and duplicate call/result IDs.

Parallel calls are matched by ID, not by their position next to a result. Boolean and numeric arguments are distinct; object key ordering does not change the repeat signature.

## Minimal format

One JSON event per line:

```jsonl
{"type":"tool_call","id":"c1","tool":"lookup","arguments":{"query":"hello"},"timestamp_ms":0}
{"type":"tool_result","id":"c1","status":"ok","timestamp_ms":25}
```

`arguments` and timestamps are optional. Event IDs connect each call to its result. See [the complete format and timing rules](docs/trace-format.md).

This is a project-specific normalized format. The optional [LangChain callback handler](docs/langchain.md) records tool calls into this format. Native OpenTelemetry, LangSmith, vendor SDK, and browser-agent export files are **not** parsed directly. Tool output bodies and hidden reasoning are not needed by this format.

## Exit codes and thresholds

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-calls` | 50 | Highest allowed call count, zero allowed |
| `--max-errors` | 0 | Highest allowed error-result count, including orphan errors |
| `--max-duration-ms` | unset | Highest allowed **observed timestamp span**, not guaranteed full run time |
| `--repeat-threshold` | 3 | Same tool and arguments count that triggers a warning; at least 2 |
| `--fail-on` | error | `error`, `warning` (includes errors), or `never` |
| `--format` | text | `text`, `json`, or `markdown` |

Exit **0** below the selected threshold, **1** when findings reach it, **2** on invalid input, arguments, or file errors. `--fail-on never` does not hide invalid input. `--output` writes to a file instead of stdout. Use `-` as the input path for stdin.

`--output` rejects the input trace itself, including hard-link and symbolic-link aliases, with exit code **2** before writing a report. An unrelated existing report can be replaced.

The error budget is zero by default, so even a recovered tool error can fail CI. Set a budget appropriate to the task. Repeated calls and pending results are warnings and do not fail the default threshold on their own.

## Python API

```python
from agent_tracecheck import analyze_trace

report = analyze_trace([
    {"type": "tool_call", "id": "c1", "tool": "lookup"},
    {"type": "tool_result", "id": "c1", "status": "ok"},
], max_calls=10, max_errors=0)
print(report["summary"])
```

## Record your own tool calls

```python
from agent_tracecheck.recording import TraceRecorder

notes = {"demo": "Release checklist"}
with TraceRecorder("trace.jsonl") as recorder:
    value = recorder.call("lookup", {"note_id": "demo"}, lambda: notes["demo"])
```

The recorder creates a **new** file and refuses to overwrite an existing path. It records argument snapshots, timing, and `ok`/`error` status; it returns the tool's actual value and re-raises its actual exception. It does not record tool outputs or exception messages. Calls can run concurrently; finish them before closing the recorder. Argument values are not automatically redacted.

For asynchronous tools, use `await recorder.acall("lookup", arguments, lambda: async_lookup(...))`. The callable must return an awaitable. Cancellation is recorded as an error and propagated to the caller. Synchronous `call` rejects awaitable results with a message directing you to `acall`; it does not cancel externally owned futures.

For a reproducible local demonstration, run `python examples/record_demo.py demo-trace.jsonl`, then `agent-tracecheck demo-trace.jsonl`. The demonstration uses scripted dictionary lookups with intentional failures, not an LLM agent or a model benchmark.

## Record LangChain tools

Install the optional integration from this checkout:

```sh
python -m pip install ".[langchain]"
python examples/langchain_demo.py langchain-trace.jsonl
agent-tracecheck langchain-trace.jsonl --fail-on never
```

The example invokes actual local LangChain tools, including a deliberately failing tool; it uses no model or API key. The handler also works with asynchronous tool callbacks:

```python
from agent_tracecheck.integrations.langchain import LangChainTraceHandler

with LangChainTraceHandler("trace.jsonl") as handler:
    result = your_tool.invoke({"query": "hello"}, config={"callbacks": [handler]})
```

This records observed tool starts, results and errors. It does not record output bodies, exception details, model events or parent/child relationships. Arguments are recorded by default and may contain sensitive values; use `argument_filter` to redact a snapshot before writing. Finish all tools before closing the handler. See [supported versions, field mapping and handled-error limits](docs/langchain.md).

## Limitations

Diagnostics depend on the completeness and correctness of your recording. Partial timestamps can underestimate real duration; no timestamps means duration cannot be checked. Repeated calls may be intentional. Results arriving before their calls in file order can be matched, but retry inference needs evidence about order. The checker does not evaluate answer quality, token usage, cost, correctness of tool behavior, or security.

Reports can contain tool names, IDs, and argument values from your trace. Keep sensitive traces and generated reports private. The program itself makes no network requests.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Related project: [mcp-changecheck](https://github.com/L1Huash1/mcp-changecheck) checks tool contract changes before a run; this project examines recorded tool activity after a run. Both operate offline.

MIT licensed. Initial development used AI coding assistance. Early alpha; see the documented limits before adopting CI thresholds.
