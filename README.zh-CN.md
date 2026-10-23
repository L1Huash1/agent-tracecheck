# agent-tracecheck

**看看 Agent 做事时，是否反复失败、留下未完成调用，或者超过调用预算。**

[English](README.md) · [数据格式](docs/trace-format.md)

假设你让 Agent 查笔记并整理摘要，它连续三次用同样的参数查询、三次都失败，最后连保存结果也没有完成。只看最终回答，很难知道中间出了什么问题。

这个工具读取已经保存的调用记录，列出这些值得检查的信号。它是离线诊断工具，需要 Python 3.10+，运行时零第三方依赖，不需要 API Key。

## 体验示例

```sh
git clone https://github.com/L1Huash1/agent-tracecheck.git
cd agent-tracecheck
python -m pip install .
agent-tracecheck examples/healthy.jsonl
agent-tracecheck examples/retry-loop.jsonl
```

第一个人工构造的示例正常通过；第二个示例故意包含重复失败和未完成调用，默认返回退出码 1，这是预期结果。

```sh
# 只查看完整 JSON 报告
agent-tracecheck examples/retry-loop.jsonl --format json --fail-on never

# 最多调用20次，最多出现1次工具错误
agent-tracecheck trace.jsonl --max-calls 20 --max-errors 1

# 连警告也要让 CI 失败
agent-tracecheck trace.jsonl --fail-on warning
```

默认允许最多 50 次工具调用、0 次错误；同一工具及参数出现至少 3 次时提示重复。可以自行调整。程序不会执行、重放这些工具，也不会判断大模型的回答是否正确。

## 数据从哪里来

首版使用自己的简化 JSONL 格式，每行一条记录：

```jsonl
{"type":"tool_call","id":"c1","tool":"lookup","arguments":{"query":"hello"},"timestamp_ms":0}
{"type":"tool_result","id":"c1","status":"ok","timestamp_ms":25}
```

调用和结果通过同一个 `id` 对应，可以处理并行调用。暂时没有直接导入各家 Agent 框架日志的功能，需要先把记录映射成此格式。示例数据是人工构造的，并非真实模型运行的性能证明。

时间字段可选。如果只有部分事件带时间，报告中的时间跨度可能低估完整耗时；没有足够时间信息时，不会虚构耗时。重复调用也可能是任务本身需要，只表示值得核对。

详细字段、退出码和能力边界见 [英文说明](README.md)。

## 记录自己的工具调用

```python
from agent_tracecheck.recording import TraceRecorder

notes = {"demo": "发布检查清单"}
with TraceRecorder("trace.jsonl") as recorder:
    value = recorder.call("lookup", {"note_id": "demo"}, lambda: notes["demo"])
```

记录器会新建文件，拒绝覆盖已有记录。它保存参数、时间及成功/失败状态，不保存工具返回内容或异常详情；参数中的敏感信息需要调用者自行处理。可以运行 `python examples/record_demo.py demo-trace.jsonl` 体验真实的本地函数调用记录，再交给 CLI 检查。演示是脚本化字典查询，不是大模型能力测试。

异步工具使用 `await recorder.acall("lookup", arguments, lambda: async_lookup(...))`。它支持并发和取消传播；所有调用结束后再关闭记录器。

配套项目 [mcp-changecheck](https://github.com/L1Huash1/mcp-changecheck) 负责检查工具升级时接口有没有变；本项目负责检查实际记录中的工具调用过程。
