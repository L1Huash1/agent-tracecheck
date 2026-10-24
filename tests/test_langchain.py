"""Exercise real LangChain tools without a model, credentials, or a server."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_tracecheck.cli import load_trace
from agent_tracecheck.core import TraceError, analyze_trace

HAS_LANGCHAIN = importlib.util.find_spec("langchain_core") is not None
if HAS_LANGCHAIN:
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import ToolException, tool
    from agent_tracecheck.integrations.langchain import LangChainTraceHandler


@unittest.skipUnless(HAS_LANGCHAIN, 'Install ".[langchain]" for integration tests')
class LangChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trace.jsonl"
        settings = patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_OTEL_ENABLED": "false",
        })
        settings.start()
        self.addCleanup(settings.stop)

    def events(self):
        return load_trace(self.path.read_text(encoding="utf-8"))

    def report(self):
        return analyze_trace(self.events(), max_errors=10)

    def test_real_sync_tool_uses_canonical_name_and_discards_output(self):
        @tool
        def lookup(query: str) -> str:
            """Look up a local note."""
            return "SECRET OUTPUT: " + query

        with LangChainTraceHandler(self.path) as handler:
            result = lookup.invoke({"query": "hello"}, config={
                "callbacks": [handler], "run_name": "display alias",
                "tags": ["private tag"], "metadata": {"private": "metadata"},
            })
        self.assertEqual(result, "SECRET OUTPUT: hello")
        events = self.events()
        self.assertEqual(events[0]["tool"], "lookup")
        self.assertEqual(events[0]["arguments"], {"query": "hello"})
        self.assertEqual(events[0]["id"], events[1]["id"])
        self.assertEqual(events[1]["status"], "ok")
        self.assertEqual(self.report()["summary"]["calls"], 1)
        text = self.path.read_text(encoding="utf-8")
        for secret in ("SECRET OUTPUT", "private tag", "metadata", "display alias"):
            self.assertNotIn(secret, text)

    def test_real_tool_exception_is_recorded_without_details_and_propagated(self):
        expected = ValueError("private exception details")

        @tool
        def fail(query: str) -> str:
            """Deliberately fail a local lookup."""
            raise expected

        with LangChainTraceHandler(self.path) as handler:
            with self.assertRaises(ValueError) as caught:
                fail.invoke({"query": "missing"}, config={"callbacks": [handler]})
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.report()["summary"]["errors"], 1)
        self.assertNotIn("private exception", self.path.read_text(encoding="utf-8"))

    def test_raw_string_input_is_not_guessed_to_be_json(self):
        @tool
        def echo(query: str) -> str:
            """Return a local input string."""
            return query

        raw = '{"looks": "like JSON"}'
        with LangChainTraceHandler(self.path) as handler:
            self.assertEqual(echo.invoke(raw, config={"callbacks": [handler]}), raw)
        self.assertEqual(self.events()[0]["arguments"], raw)

    def test_real_threaded_calls_are_paired_by_run_id(self):
        rendezvous = threading.Barrier(3)

        @tool
        def parallel(number: int) -> int:
            """Return the local worker number after rendezvous."""
            rendezvous.wait(timeout=5)
            return number

        with LangChainTraceHandler(self.path) as handler:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(parallel.invoke, {"number": number},
                                       config={"callbacks": [handler]}) for number in range(3)]
                self.assertEqual([future.result(timeout=10) for future in futures], [0, 1, 2])
        events = self.events()
        self.assertTrue(all(event["type"] == "tool_call" for event in events[:3]))
        self.assertEqual(len({event["id"] for event in events[:3]}), 3)
        summary = self.report()["summary"]
        self.assertEqual((summary["calls"], summary["errors"], summary["pending"]), (3, 0, 0))

    def test_real_async_tools_support_concurrent_calls(self):
        @tool
        async def async_lookup(number: int) -> int:
            """Resolve a local asynchronous lookup."""
            await asyncio.sleep(0)
            return number

        async def run(handler):
            return await asyncio.gather(*(async_lookup.ainvoke(
                {"number": number}, config={"callbacks": [handler]}) for number in range(3)))

        with LangChainTraceHandler(self.path) as handler:
            self.assertEqual(asyncio.run(run(handler)), [0, 1, 2])
        self.assertEqual(self.report()["summary"]["calls"], 3)
        self.assertEqual(self.report()["summary"]["pending"], 0)

    def test_real_async_error_propagates_and_records_error(self):
        @tool
        async def async_fail(query: str) -> str:
            """Raise a local asynchronous error."""
            raise RuntimeError("not persisted")

        with LangChainTraceHandler(self.path) as handler:
            with self.assertRaisesRegex(RuntimeError, "not persisted"):
                asyncio.run(async_fail.ainvoke({"query": "x"}, config={"callbacks": [handler]}))
        self.assertEqual(self.report()["summary"]["errors"], 1)
        self.assertNotIn("not persisted", self.path.read_text(encoding="utf-8"))

    def test_handled_error_tool_message_is_an_error(self):
        @tool
        def handled(query: str) -> str:
            """Raise an error which LangChain handles."""
            raise ToolException("private handled failure")

        handled.handle_tool_error = True
        with LangChainTraceHandler(self.path) as handler:
            result = handled.invoke({"type": "tool_call", "id": "external-id",
                                     "name": "handled", "args": {"query": "x"}},
                                    config={"callbacks": [handler]})
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.report()["summary"]["errors"], 1)
        self.assertNotIn("private handled", self.path.read_text(encoding="utf-8"))

    def test_handled_plain_text_error_cannot_be_inferred(self):
        @tool
        def handled(query: str) -> str:
            """Handle a failure as plain text without an error ToolMessage."""
            raise ToolException("error response")

        handled.handle_tool_error = True
        with LangChainTraceHandler(self.path) as handler:
            self.assertEqual(handled.invoke({"query": "x"}, config={"callbacks": [handler]}),
                             "error response")
        self.assertEqual(self.events()[-1]["status"], "ok")

    def test_arbitrary_status_error_dictionary_is_not_inferred_as_failure(self):
        @tool
        def status() -> dict:
            """Return a dictionary which is ordinary tool data."""
            return {"status": "error"}

        with LangChainTraceHandler(self.path) as handler:
            status.invoke({}, config={"callbacks": [handler]})
        self.assertEqual(self.events()[-1]["status"], "ok")

    def test_argument_filter_cannot_mutate_actual_tool_inputs(self):
        def redact(arguments):
            arguments["values"][0] = "[redacted]"
            return arguments

        @tool
        def identity(values: list[str]) -> list[str]:
            """Return the original tool arguments."""
            return values

        arguments = {"values": ["secret"]}
        with LangChainTraceHandler(self.path, argument_filter=redact) as handler:
            self.assertEqual(identity.invoke(arguments, config={"callbacks": [handler]}), ["secret"])
        self.assertEqual(arguments, {"values": ["secret"]})
        self.assertEqual(self.events()[0]["arguments"], {"values": ["[redacted]"]})

    def test_invalid_filtered_arguments_stop_tool_before_execution(self):
        executed = []

        @tool
        def observe(query: str) -> str:
            """Observe whether an invalid callback permits execution."""
            executed.append(query)
            return query

        with LangChainTraceHandler(self.path, argument_filter=lambda _: {"bad": object()}) as handler:
            with self.assertRaises(TraceError):
                observe.invoke({"query": "x"}, config={"callbacks": [handler]})
        self.assertEqual(executed, [])
        self.assertEqual(self.events(), [])

    def test_filter_exception_is_visible_and_preserves_empty_trace(self):
        expected = RuntimeError("redactor failed")

        def fail_filter(arguments):
            raise expected

        with LangChainTraceHandler(self.path, argument_filter=fail_filter) as handler:
            with self.assertRaises(RuntimeError) as caught:
                handler.on_tool_start({"name": "lookup"}, "x", run_id=uuid4())
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.events(), [])

    def test_existing_file_is_never_overwritten(self):
        self.path.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            LangChainTraceHandler(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "keep")

    def test_duplicate_and_unknown_callbacks_are_rejected(self):
        run_id = uuid4()
        with LangChainTraceHandler(self.path) as handler:
            handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
            with self.assertRaisesRegex(TraceError, "duplicate"):
                handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
            with self.assertRaisesRegex(TraceError, "unknown"):
                handler.on_tool_end("ignored", run_id=uuid4())
            handler.on_tool_end("ignored", run_id=run_id)
            with self.assertRaisesRegex(TraceError, "already completed"):
                handler.on_tool_error(ValueError(), run_id=run_id)
            with self.assertRaisesRegex(TraceError, "duplicate"):
                handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
        self.assertEqual(len(self.events()), 2)

    def test_default_close_refuses_pending_then_can_finish(self):
        handler = LangChainTraceHandler(self.path)
        self.addCleanup(handler.close, allow_pending=True)
        run_id = uuid4()
        handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
        with self.assertRaisesRegex(RuntimeError, "in flight"):
            handler.close()
        handler.on_tool_end("ignored", run_id=run_id)
        handler.close()
        handler.close()
        self.assertEqual(self.report()["summary"]["pending"], 0)

    def test_explicit_partial_close_does_not_invent_results(self):
        handler = LangChainTraceHandler(self.path)
        run_id = uuid4()
        handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
        handler.close(allow_pending=True)
        handler.close(allow_pending=True)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.report()["summary"]["pending"], 1)
        with self.assertRaises(TraceError):
            handler.on_tool_end("late", run_id=run_id)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            handler.on_tool_start({"name": "lookup"}, "x", run_id=uuid4())

    def test_real_async_cancellation_remains_cancelled_and_trace_is_partial_or_error(self):
        handler = LangChainTraceHandler(self.path)
        self.addCleanup(handler.close, allow_pending=True)

        async def run():
            started = asyncio.Event()

            @tool
            async def wait(query: str) -> str:
                """Wait until the caller cancels this local tool."""
                started.set()
                await asyncio.Future()
                return query

            task = asyncio.create_task(wait.ainvoke({"query": "x"}, config={"callbacks": [handler]}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        handler.close(allow_pending=True)
        summary = self.report()["summary"]
        self.assertEqual(summary["calls"], 1)
        # Framework versions may emit on_tool_error for cancellation or omit it.
        self.assertEqual(summary["pending"] + summary["errors"], 1)

    def test_non_tool_callbacks_are_ignored(self):
        with LangChainTraceHandler(self.path) as handler:
            handler.on_chain_start({"name": "chain"}, {"private": "input"}, run_id=uuid4())
            handler.on_chain_end({"private": "output"}, run_id=uuid4())
        self.assertEqual(self.events(), [])

    def test_recording_failure_stops_real_tool_before_execution(self):
        executed = []

        @tool
        def observe(query: str) -> str:
            """Observe whether a recording failure permits execution."""
            executed.append(query)
            return query

        handler = LangChainTraceHandler(self.path)
        self.addCleanup(handler.close, allow_pending=True)
        handler._recorder._stream.close()
        with self.assertRaises(ValueError):
            observe.invoke({"query": "x"}, config={"callbacks": [handler]})
        self.assertEqual(executed, [])

    def test_tool_error_is_preserved_if_recording_its_error_also_fails(self):
        expected = RuntimeError("original private tool failure")
        handler = LangChainTraceHandler(self.path)
        self.addCleanup(handler.close, allow_pending=True)

        @tool
        def fail(query: str) -> str:
            """Fail after the trace stream becomes unavailable."""
            handler._recorder._stream.close()
            raise expected

        with self.assertRaises(RuntimeError) as caught:
            fail.invoke({"query": "x"}, config={"callbacks": [handler]})
        self.assertIs(caught.exception, expected)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        handler.close()
        self.assertEqual(self.report()["summary"]["pending"], 1)
        self.assertNotIn("original private", self.path.read_text(encoding="utf-8"))

    def test_invalid_options_and_callback_metadata_do_not_write_events(self):
        with self.assertRaises(TypeError):
            LangChainTraceHandler(self.path, argument_filter="invalid")
        self.assertFalse(self.path.exists())
        with LangChainTraceHandler(self.path) as handler:
            for run_id in (None, 3, ""):
                with self.subTest(run_id=run_id), self.assertRaises(TraceError):
                    handler.on_tool_start({"name": "lookup"}, "x", run_id=run_id)
            with self.assertRaises(TraceError):
                handler.on_tool_start({"name": " "}, "x", run_id=uuid4())
            with self.assertRaises(TypeError):
                handler.close(allow_pending="yes")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
