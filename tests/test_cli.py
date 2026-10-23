"""Real-process CLI regressions, plus the report renderer's safety contract."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_tracecheck.cli import render_report
from agent_tracecheck.core import analyze_trace


PROJECT = Path(__file__).resolve().parents[1]


def healthy_events():
    return [{"type": "tool_call", "id": "a", "tool": "search", "arguments": {"q": "notes"},
             "timestamp_ms": 10},
            {"type": "tool_result", "id": "a", "status": "ok", "timestamp_ms": 15}]


def to_jsonl(events):
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.trace = self.directory / "trace.jsonl"
        self.events = healthy_events()
        self.write_events(self.events)

    def write_events(self, events):
        self.trace.write_text(to_jsonl(events), encoding="utf-8")

    def run_cli(self, *arguments, input_text=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run([sys.executable, "-m", "agent_tracecheck", *map(str, arguments)],
                              cwd=self.directory, env=env, input=input_text,
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def inspect(self, *options):
        return self.run_cli(self.trace, *options)

    def assert_user_error(self, completed):
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertTrue(completed.stderr.strip())
        self.assertNotIn("Traceback", completed.stderr)

    def test_version_needs_no_trace_path(self):
        completed = self.run_cli("--version")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("agent-tracecheck", completed.stdout)
        self.assertIn("0.1.0", completed.stdout)

    def test_healthy_json_report_matches_core_report_exactly(self):
        completed = self.inspect("--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), analyze_trace(self.events))
        self.assertEqual(completed.stderr, "")

    def test_text_and_markdown_reports_include_observed_counts(self):
        for format_name in ("text", "markdown"):
            with self.subTest(format_name=format_name):
                completed = self.inspect("--format", format_name)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("1 calls", completed.stdout)
                self.assertIn("0 errors", completed.stdout)
                self.assertIn("5 ms", completed.stdout)

    def test_stdin_is_supported_and_report_matches_file_input(self):
        completed = self.run_cli("-", "--format", "json", input_text=to_jsonl(self.events))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), analyze_trace(self.events))

    def test_empty_and_blank_trace_are_valid(self):
        for text in ("", "\n\n  \n\t\r\n"):
            with self.subTest(text=text):
                self.trace.write_text(text, encoding="utf-8")
                completed = self.inspect("--format", "json")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout), analyze_trace([]))

    def test_file_and_stdin_accept_utf8_bom(self):
        self.trace.write_text(to_jsonl(self.events), encoding="utf-8-sig")
        completed = self.inspect("--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), analyze_trace(self.events))
        completed = self.run_cli("-", "--format", "json", input_text="\ufeff" + to_jsonl(self.events))
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_calls_errors_and_duration_budget_excess_returns_one(self):
        cases = (("--max-calls", "0"), ("--max-duration-ms", "4"))
        for options in cases:
            with self.subTest(options=options):
                completed = self.inspect(*options, "--format", "json")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertTrue(any(f["severity"] == "error" for f in json.loads(completed.stdout)["findings"]))
        self.events[1]["status"] = "error"
        self.write_events(self.events)
        self.assertEqual(self.inspect().returncode, 1)

    def test_budget_boundaries_are_allowed(self):
        completed = self.inspect("--max-calls", "1", "--max-duration-ms", "5")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_allowing_error_budget_still_reports_failed_call_warning(self):
        self.events[1]["status"] = "error"
        self.write_events(self.events)
        completed = self.inspect("--max-errors", "1", "--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertTrue(any(f["severity"] == "warning" for f in report["findings"]))
        self.assertEqual(self.inspect("--max-errors", "1", "--fail-on", "warning").returncode, 1)

    def test_repetition_warning_respects_fail_threshold_and_repeat_threshold(self):
        events = []
        for index in range(3):
            events.extend([{"type": "tool_call", "id": str(index), "tool": "search"},
                           {"type": "tool_result", "id": str(index), "status": "ok"}])
        self.write_events(events)
        completed = self.inspect("--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(any(f["code"] == "repeated_call" for f in json.loads(completed.stdout)["findings"]))
        self.assertEqual(self.inspect("--fail-on", "warning").returncode, 1)
        self.assertEqual(self.inspect("--fail-on", "never").returncode, 0)
        self.assertEqual(self.inspect("--repeat-threshold", "4", "--fail-on", "warning").returncode, 0)

    def test_missing_result_warning_is_distinct_from_orphan_error(self):
        self.write_events([{"type": "tool_call", "id": "a", "tool": "search"}])
        self.assertEqual(self.inspect().returncode, 0)
        self.assertEqual(self.inspect("--fail-on", "warning").returncode, 1)
        self.write_events([{"type": "tool_result", "id": "a", "status": "ok"}])
        self.assertEqual(self.inspect().returncode, 1)

    def test_never_threshold_does_not_hide_invalid_input(self):
        self.trace.write_text("{broken\n", encoding="utf-8")
        self.assert_user_error(self.inspect("--fail-on", "never"))

    def test_report_file_has_same_report_and_stdout_is_empty(self):
        target = self.directory / "report.json"
        completed = self.inspect("--format", "json", "--output", target)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), analyze_trace(self.events))

    def test_failed_budget_still_writes_report_file(self):
        target = self.directory / "report.json"
        completed = self.inspect("--max-calls", "0", "--format", "json", "--output", target)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertTrue(any(f["severity"] == "error" for f in json.loads(target.read_text(encoding="utf-8"))["findings"]))

    def test_output_cannot_overwrite_input(self):
        original = self.trace.read_bytes()
        self.assert_user_error(self.inspect("--output", self.trace))
        self.assertEqual(self.trace.read_bytes(), original)

    def test_invalid_output_path_returns_diagnostic_without_traceback(self):
        self.assert_user_error(self.inspect("--output", self.directory))

    def test_missing_file_and_invalid_utf8_return_diagnostic(self):
        self.trace.unlink()
        self.assert_user_error(self.inspect())
        self.trace.write_bytes(b"\xff\xfe\xff")
        self.assert_user_error(self.inspect())

    def test_duplicate_json_keys_are_rejected_including_nested_arguments(self):
        for text in (
            '{"type":"tool_call","id":"a","id":"b","tool":"search"}',
            '{"type":"tool_call","id":"a","tool":"search","arguments":{"q":1,"q":2}}',
        ):
            with self.subTest(text=text):
                self.trace.write_text(text, encoding="utf-8")
                self.assert_user_error(self.inspect())

    def test_nonfinite_numbers_and_unroundtrippable_decimals_are_rejected(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e400", "1e-400",
                      "0.123456789012345678901", "1.0000000000000001"):
            text = '{"type":"tool_call","id":"a","tool":"search","arguments":{"n":' + token + '}}'
            with self.subTest(token=token):
                self.trace.write_text(text, encoding="utf-8")
                self.assert_user_error(self.inspect())

    def test_roundtrippable_decimal_tokens_are_accepted(self):
        for token in ("1.2300000000000000", "1e100", "0.1"):
            text = '{"type":"tool_call","id":"a","tool":"search","arguments":{"n":' + token + '}}'
            self.trace.write_text(text + '\n{"type":"tool_result","id":"a","status":"ok"}\n', encoding="utf-8")
            with self.subTest(token=token):
                completed = self.inspect("--format", "json")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["summary"]["calls"], 1)

    def test_unicode_paths_and_values_are_preserved(self):
        self.trace = self.directory / "运行轨迹.jsonl"
        self.events[0]["tool"] = "搜索🔎"
        self.events[0]["arguments"] = {"问题": "你好，世界🌏"}
        self.write_events(self.events)
        completed = self.inspect("--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), analyze_trace(self.events))

    def test_legal_unicode_line_separators_inside_strings_are_not_jsonl_delimiters(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            self.events[0]["arguments"] = {"text": "left" + separator + "right"}
            self.write_events(self.events)
            with self.subTest(separator=repr(separator)):
                completed = self.inspect("--format", "json")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout), analyze_trace(self.events))

    def test_json_diagnostic_identifies_physical_line(self):
        self.trace.write_text(json.dumps(self.events[0]) + "\n\n{broken\n", encoding="utf-8")
        completed = self.inspect()
        self.assert_user_error(completed)
        self.assertIn("Line 3", completed.stderr)

    def test_invalid_event_shapes_and_duplicate_ids_return_diagnostic(self):
        for events in ([[]], [{"type": "message", "id": "a"}], [self.events[0], self.events[0]]):
            with self.subTest(events=events):
                self.write_events(events)
                self.assert_user_error(self.inspect())

    def test_invalid_budget_options_are_usage_errors(self):
        for option, value in (("--max-calls", "-1"), ("--max-errors", "-1"),
                              ("--repeat-threshold", "1"), ("--max-duration-ms", "nan"),
                              ("--max-duration-ms", "inf"), ("--fail-on", "sometimes")):
            with self.subTest(option=option, value=value):
                self.assert_user_error(self.inspect(option, value))

    def test_hostile_trace_metadata_stays_inert_in_markdown(self):
        self.events[0]["tool"] = "<script>|*danger*\nFORGED\t\x1b"
        self.write_events([self.events[0]])
        completed = self.inspect("--format", "markdown")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("<script>", completed.stdout)
        self.assertNotIn("\nFORGED", completed.stdout)
        self.assertNotIn("\t", completed.stdout)
        self.assertNotIn("\x1b", completed.stdout)

    def test_markdown_renderer_escapes_finding_markup_and_controls(self):
        report = analyze_trace([])
        report["summary"]["findings"] = 1
        report["findings"] = [{"severity": "warning", "code": "example",
                               "message": "<script>|*danger*\nFORGED\t\x1b"}]
        rendered = render_report(report, "markdown")
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn(r"\|", rendered)
        self.assertIn(r"\*danger\*", rendered)
        self.assertNotIn("\nFORGED", rendered)
        self.assertNotIn("\t", rendered)
        self.assertNotIn("\x1b", rendered)


if __name__ == "__main__":
    unittest.main()
