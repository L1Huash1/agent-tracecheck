"""Recorder behavior, data boundaries, and concurrent event pairing."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_tracecheck.core import TraceError, analyze_trace
from agent_tracecheck.recording import TraceRecorder


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "trace.jsonl"

    def events(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_result_identity_is_preserved_but_output_is_not_recorded(self):
        output = {"private_output": object()}
        with TraceRecorder(self.path) as recorder:
            actual = recorder.call("lookup", {"query": "demo"}, lambda: output)
        self.assertIs(actual, output)
        self.assertNotIn("private_output", self.path.read_text(encoding="utf-8"))
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(set(events[1]), {"type", "id", "timestamp_ms", "status"})
        self.assertEqual(events[0]["id"], events[1]["id"])
        self.assertEqual(events[1]["status"], "ok")
        self.assertEqual(analyze_trace(events)["summary"]["pending"], 0)

    def test_exception_identity_propagates_without_details_in_trace(self):
        original = LookupError("secret exception detail")

        def fail():
            raise original

        with self.assertRaises(LookupError) as raised:
            with TraceRecorder(self.path) as recorder:
                recorder.call("lookup", {}, fail)
        self.assertIs(raised.exception, original)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("LookupError", text)
        self.assertEqual(self.events()[1]["status"], "error")

    def test_base_exceptions_are_recorded_and_rethrown(self):
        def stop():
            raise KeyboardInterrupt()

        with TraceRecorder(self.path) as recorder:
            with self.assertRaises(KeyboardInterrupt):
                recorder.call("stop", {}, stop)
        self.assertEqual(self.events()[1]["status"], "error")

    def test_arguments_are_snapshotted_before_callback_mutation(self):
        arguments = {"nested": [{"enabled": True, "value": None}], "number": 1.5}

        def mutate():
            arguments["nested"][0]["enabled"] = False
            arguments["new"] = "later"

        with TraceRecorder(self.path) as recorder:
            recorder.call("mutate", arguments, mutate)
        arguments["nested"].append("after close")
        self.assertEqual(self.events()[0]["arguments"], {
            "nested": [{"enabled": True, "value": None}], "number": 1.5})

    def test_parallel_calls_run_outside_lock_and_pair_with_unique_ids(self):
        count = 8
        barrier = threading.Barrier(count)

        def work(index, recorder):
            def tool():
                barrier.wait(timeout=5)
                return index
            return recorder.call("parallel_lookup", {"index": index}, tool)

        with TraceRecorder(self.path) as recorder:
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(work, index, recorder) for index in range(count)]
                self.assertEqual(sorted(future.result(timeout=8) for future in futures), list(range(count)))
        events = self.events()
        calls = [event for event in events if event["type"] == "tool_call"]
        results = [event for event in events if event["type"] == "tool_result"]
        self.assertEqual(len({event["id"] for event in calls}), count)
        self.assertEqual({event["id"] for event in calls}, {event["id"] for event in results})
        timestamps = [event["timestamp_ms"] for event in events]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(all(value >= 0 for value in timestamps))
        summary = analyze_trace(events)["summary"]
        self.assertEqual((summary["calls"], summary["results"], summary["pending"]), (count, count, 0))

    def test_nested_calls_do_not_deadlock(self):
        with TraceRecorder(self.path) as recorder:
            result = recorder.call("outer", {}, lambda: recorder.call("inner", {}, lambda: 7))
        self.assertEqual(result, 7)
        self.assertEqual(analyze_trace(self.events())["summary"]["pending"], 0)

    def test_exclusive_creation_never_overwrites_existing_file(self):
        self.path.write_text("keep this", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            TraceRecorder(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "keep this")

    def test_missing_parent_directories_are_created(self):
        path = Path(self.directory.name) / "new" / "nested" / "trace.jsonl"
        with TraceRecorder(path) as recorder:
            recorder.call("lookup", None, lambda: 1)
        self.assertTrue(path.is_file())

    def test_invalid_tools_arguments_and_callbacks_do_not_execute(self):
        executed = []
        callback = lambda: executed.append(True)
        circular = []
        circular.append(circular)
        with TraceRecorder(self.path) as recorder:
            for tool in (None, "", " \t ", 1, {}, []):
                with self.subTest(tool=tool), self.assertRaises(TraceError):
                    recorder.call(tool, {}, callback)
            for arguments in ({"x": float("nan")}, float("inf"), {1: "bad key"},
                              (1, 2), {"set"}, object(), circular):
                with self.subTest(arguments=type(arguments).__name__), self.assertRaises(TraceError):
                    recorder.call("lookup", arguments, callback)
            with self.assertRaises(TypeError):
                recorder.call("lookup", {}, None)
        self.assertEqual(executed, [])
        self.assertEqual(self.events(), [])

    def test_close_rejects_inflight_calls_then_succeeds_after_completion(self):
        started, release = threading.Event(), threading.Event()
        recorder = TraceRecorder(self.path)
        self.addCleanup(recorder.close)

        def blocked():
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test worker did not receive release")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(recorder.call, "blocked", {}, blocked)
            try:
                self.assertTrue(started.wait(timeout=3))
                with self.assertRaisesRegex(RuntimeError, "in flight"):
                    recorder.close()
            finally:
                release.set()
            future.result(timeout=5)
        recorder.close()
        recorder.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            recorder.call("late", {}, lambda: self.fail("closed recorder ran tool"))
        self.assertEqual(len(self.events()), 2)

    def test_tool_exception_still_propagates_if_result_recording_fails(self):
        original = LookupError("original")

        def fail():
            raise original

        with TraceRecorder(self.path) as recorder:
            original_write = recorder._write

            def write(event):
                if event["type"] == "tool_result":
                    raise OSError("recording unavailable")
                original_write(event)

            with patch.object(recorder, "_write", side_effect=write):
                with self.assertRaises(LookupError) as raised:
                    recorder.call("lookup", {}, fail)
        self.assertIs(raised.exception, original)
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_failed_call_write_does_not_run_tool_or_allow_further_recording(self):
        executed = []
        with TraceRecorder(self.path) as recorder:
            with patch.object(recorder._stream, "write", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    recorder.call("lookup", {}, lambda: executed.append(True))
            with self.assertRaisesRegex(RuntimeError, "recording failure"):
                recorder.call("lookup", {}, lambda: executed.append(True))
        self.assertEqual(executed, [])
        self.assertEqual(self.events(), [])

    def test_async_result_identity_is_preserved_without_recording_output(self):
        output = {"private_async_output": object()}

        async def run():
            async def lookup():
                await asyncio.sleep(0)
                return output
            with TraceRecorder(self.path) as recorder:
                return await recorder.acall("async_lookup", {"query": "demo"}, lookup)

        self.assertIs(asyncio.run(run()), output)
        self.assertNotIn("private_async_output", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.events()[1]["status"], "ok")

    def test_async_exception_identity_propagates_without_details(self):
        original = LookupError("private async exception detail")

        async def run():
            async def fail():
                raise original
            with TraceRecorder(self.path) as recorder:
                await recorder.acall("async_lookup", {}, fail)

        with self.assertRaises(LookupError) as raised:
            asyncio.run(run())
        self.assertIs(raised.exception, original)
        self.assertNotIn("private async", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.events()[1]["status"], "error")

    def test_async_concurrency_records_independent_matching_calls(self):
        count = 6

        async def run():
            ready = asyncio.Event()
            started = 0

            async def lookup(index):
                nonlocal started
                started += 1
                if started == count:
                    ready.set()
                await ready.wait()
                await asyncio.sleep(0)
                return index

            with TraceRecorder(self.path) as recorder:
                return await asyncio.wait_for(asyncio.gather(*(
                    recorder.acall("async_lookup", {"index": index}, lambda i=index: lookup(i))
                    for index in range(count))), timeout=5)

        self.assertEqual(asyncio.run(run()), list(range(count)))
        events = self.events()
        self.assertTrue(all(event["type"] == "tool_call" for event in events[:count]))
        summary = analyze_trace(events)["summary"]
        self.assertEqual((summary["calls"], summary["results"], summary["pending"]), (count, count, 0))
        self.assertEqual(len({event["id"] for event in events}), count)

    def test_async_cancellation_records_error_and_preserves_cancelled_error(self):
        inside_errors = []

        async def run():
            started, release = asyncio.Event(), asyncio.Event()

            async def blocked():
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError as error:
                    inside_errors.append(error)
                    raise

            with TraceRecorder(self.path) as recorder:
                task = asyncio.create_task(recorder.acall("blocked", {}, blocked))
                await started.wait()
                task.cancel("caller cancelled")
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await task
                self.assertIs(raised.exception, inside_errors[0])

        asyncio.run(run())
        self.assertEqual(self.events()[1]["status"], "error")
        self.assertEqual(analyze_trace(self.events())["summary"]["pending"], 0)
        self.assertNotIn("caller cancelled", self.path.read_text(encoding="utf-8"))

    def test_sync_call_rejects_and_closes_unstarted_coroutine(self):
        async def lookup():
            return "not executed"

        async def run():
            coroutine = lookup()
            with TraceRecorder(self.path) as recorder:
                with self.assertRaisesRegex(TypeError, "acall"):
                    recorder.call("async_lookup", {}, lambda: coroutine)
            self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)

        asyncio.run(run())
        self.assertEqual(self.events()[1]["status"], "error")

    def test_sync_call_does_not_cancel_external_future(self):
        async def run():
            future = asyncio.get_running_loop().create_future()
            with TraceRecorder(self.path) as recorder:
                with self.assertRaisesRegex(TypeError, "acall"):
                    recorder.call("future_lookup", {}, lambda: future)
            self.assertFalse(future.cancelled())
            self.assertFalse(future.done())
            future.set_result("still usable")
            self.assertEqual(await future, "still usable")

        asyncio.run(run())
        self.assertEqual(self.events()[1]["status"], "error")


if __name__ == "__main__":
    unittest.main()
