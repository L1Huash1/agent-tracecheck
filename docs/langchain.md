# Record LangChain tools locally

The optional integration records observed LangChain tool callbacks into the same JSONL format as the base CLI. It uses real framework tools without requiring a language model, API key, LangSmith account or server. It is a callback recorder, not a native LangSmith export importer or a full agent trace collector.

## Install and try

From the repository checkout:

```sh
python -m pip install ".[langchain]"
python examples/langchain_demo.py langchain-trace.jsonl
agent-tracecheck langchain-trace.jsonl --format markdown --fail-on never
```

The example performs six actual local LangChain tool calls. Three deliberately fail using the same arguments, so diagnostics include tool errors and repeated-call signals. The model task and failures are scripted, not evidence of agent or model quality. The demonstration explicitly disables LangSmith tracing in its own process.

The integration supports **langchain-core >=1.7.0,<1.8.0**, with Python 3.10+. Regression tests exercise version 1.7.0 and CI installs the newest available version in the supported range. Other framework versions are not claimed compatible. The base CLI has no runtime dependencies; only this extra installs LangChain and its dependencies. Install from source or a GitHub release; this documentation does not claim a PyPI package is published.

## Attach a handler

```python
from langchain_core.tools import tool
from agent_tracecheck.integrations.langchain import LangChainTraceHandler

@tool
def lookup(query: str) -> str:
    """Look up a local value."""
    return {"hello": "world"}[query]

with LangChainTraceHandler("trace.jsonl") as handler:
    value = lookup.invoke({"query": "hello"}, config={"callbacks": [handler]})
```

Use the same configuration with `await your_tool.ainvoke(...)`. Threaded and asynchronous calls are matched by framework run IDs, independently of completion order. All calls must finish before leaving the context. Paths are created exclusively; existing files are refused.

Pass the handler through a runnable's callback configuration only when that runnable propagates callbacks to its tools. Calls without this handler are invisible to the recorder. Other configured handlers and framework instrumentation may have their own network behavior; this handler never uploads traces.

## Field mapping and limits

| Source | Recorded value |
| --- | --- |
| `on_tool_start` | `tool_call` event |
| Framework `run_id` | Matched internally to a unique local `call-N` ID; original UUID not saved |
| `serialized["name"]` | Canonical tool name; callback `name` only used if absent |
| `inputs` when supplied | JSON argument snapshot |
| Otherwise `input_str` | Original string, without guessing JSON or parsing Python representations |
| Callback arrival time | Milliseconds since handler creation using a local monotonic clock |
| `on_tool_end` | `ok`, except a `ToolMessage(status="error")` becomes `error` |
| `on_tool_error` | `error`; original tool error remains visible to the caller |

Model events, output bodies, artifacts, exception messages and tracebacks, tags, metadata, original run UUIDs and parent/child relationships are discarded. Timing measures callback arrival, not guaranteed full agent duration. Filtering arguments can merge repeat signatures. Missing callbacks remain missing rather than being inferred.

When LangChain handles a `ToolException` or validation failure and returns an error `ToolMessage`, the handler recognizes its status. If the framework or tool converts a failure into an ordinary string or dictionary and calls `on_tool_end`, the handler cannot reliably distinguish it from success. It does not inspect output text for words such as “error.”

Only tool names and recorded argument values determine repeat signatures. Display names set through `run_name` do not change the canonical tool identity.

## Redact argument values

Arguments are recorded by default and may contain private values. The optional synchronous `argument_filter` receives a deep JSON snapshot before each call is written:

```python
def redact(arguments):
    if isinstance(arguments, dict) and "api_key" in arguments:
        arguments["api_key"] = "[redacted]"
    return arguments

with LangChainTraceHandler("trace.jsonl", argument_filter=redact) as handler:
    value = lookup.invoke({"query": "hello"}, config={"callbacks": [handler]})
```

Mutation inside the filter does not mutate actual tool inputs. The filter must return finite JSON data; invalid data or filter exceptions stop recording and propagate before tool execution. This example filters one top-level key only, not arbitrary secrets or nested data. No automatic redaction is performed.

## Partial traces, cancellation and errors

`run_inline=True` and `raise_error=True` make recording failures visible instead of letting LangChain silently continue without the expected trace. Invalid callback metadata, duplicate starts, unknown result IDs and repeated results raise an error. A disk failure can therefore interrupt the caller; tool side effects that already happened cannot be undone.

The default `close()` refuses pending calls and leaves the trace open so their end/error callbacks can finish. Join worker calls before closing. Some framework cancellation paths do not emit `on_tool_error`. After all framework tasks have **stopped**, explicitly keep a partial trace:

```python
handler.close(allow_pending=True)
```

This closes the file without fabricating results. The analyzer reports those calls as pending. Further callbacks are rejected; do not use this option while expecting calls to finish normally. Cancellation still propagates to the caller.

The implementation reuses private writer methods from this same package to share file creation, validation, snapshots and locking. They are internal implementation details rather than a public adapter API.

## Validation and references

Run the base suite without the extra, or install the extra to include the integration tests:

```sh
python -m pip install -e ".[langchain]"
python -m unittest discover -s tests -v
```

Integration tests use real `@tool` calls for synchronous, asynchronous, concurrent, failed, handled-error and cancelled operations. They also cover argument filtering, source preservation, callback identity and recording failures. No language model is called.

The mapping follows the official [callback interface](https://reference.langchain.com/python/langchain-core/callbacks/base/BaseCallbackHandler), [tool-start fields](https://reference.langchain.com/python/langchain-core/callbacks/base/CallbackManagerMixin/on_tool_start), and [tool execution implementation](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/tools/base.py). These are reference links, not a promise of compatibility with future releases.

中文：这是可选的 LangChain 工具回调记录器，能在本地记录同步、异步和并发工具调用。默认记录参数，可通过过滤函数处理敏感值；不记录返回正文和异常详情。取消或缺少回调时可保存不完整记录，由诊断工具提示未完成调用。工具内部处理成普通文本的失败无法可靠识别。
