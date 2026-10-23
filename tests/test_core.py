"""Behavioral regression tests for normalized, offline trace analysis."""

from copy import deepcopy
import math
import unittest

from agent_tracecheck.core import TraceError, analyze_trace


def call(event_id="a", tool="search", arguments=None, timestamp=None):
    event = {"type": "tool_call", "id": event_id, "tool": tool}
    if arguments is not None:
        event["arguments"] = arguments
    if timestamp is not None:
        event["timestamp_ms"] = timestamp
    return event


def result(event_id="a", status="ok", timestamp=None):
    event = {"type": "tool_result", "id": event_id, "status": status}
    if timestamp is not None:
        event["timestamp_ms"] = timestamp
    return event


def codes(report):
    return [finding["code"] for finding in report["findings"]]


class MatchingTests(unittest.TestCase):
    def test_empty_trace_has_zero_summary(self):
        report = analyze_trace([])
        self.assertEqual(report, {"schema_version": 1, "summary": {
            "calls": 0, "results": 0, "errors": 0, "pending": 0,
            "duration_ms": None, "findings": 0}, "findings": [], "calls": []})

    def test_complete_untimed_call_defaults_to_empty_arguments(self):
        report = analyze_trace([call(), result()])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["calls"], [{"id": "a", "tool": "search", "arguments": {},
                                            "status": "ok", "duration_ms": None}])

    def test_parallel_calls_match_by_id_and_keep_original_call_order(self):
        events = [call("a", timestamp=10), call("b", timestamp=15),
                  result("b", timestamp=20), result("a", timestamp=50)]
        report = analyze_trace(events)
        self.assertEqual([item["id"] for item in report["calls"]], ["a", "b"])
        self.assertEqual([item["duration_ms"] for item in report["calls"]], [40, 5])
        self.assertEqual(report["summary"]["duration_ms"], 40)
        self.assertEqual(report["findings"], [])

    def test_result_before_call_in_file_is_matched(self):
        report = analyze_trace([result("a", timestamp=30), call("a", timestamp=10)])
        self.assertEqual(report["calls"][0]["status"], "ok")
        self.assertEqual(report["calls"][0]["duration_ms"], 20)
        self.assertEqual(report["findings"], [])

    def test_pending_call_has_warning_and_null_duration(self):
        report = analyze_trace([call()])
        self.assertEqual(report["summary"]["pending"], 1)
        self.assertEqual(report["calls"][0]["status"], "pending")
        self.assertEqual(report["calls"][0]["duration_ms"], None)
        self.assertIn("missing_result", codes(report))
        self.assertEqual(report["findings"][0]["severity"], "warning")

    def test_orphan_result_is_error_finding_instead_of_schema_error(self):
        report = analyze_trace([result("missing")])
        self.assertEqual(report["summary"]["results"], 1)
        self.assertEqual(report["summary"]["calls"], 0)
        self.assertIn("orphan_result", codes(report))
        self.assertEqual(report["findings"][0]["severity"], "error")

    def test_error_result_updates_call_and_counts_toward_error_budget(self):
        report = analyze_trace([call(), result(status="error")])
        self.assertEqual(report["calls"][0]["status"], "error")
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertIn("tool_error", codes(report))
        self.assertIn("error_budget", codes(report))

    def test_orphan_errors_count_toward_budget(self):
        report = analyze_trace([result("orphan", status="error")])
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertIn("error_budget", codes(report))

    def test_report_counts_and_findings_are_consistent(self):
        report = analyze_trace([call("a"), call("b"), result("a", status="error"), result("orphan")])
        self.assertEqual(report["summary"]["calls"], 2)
        self.assertEqual(report["summary"]["results"], 2)
        self.assertEqual(report["summary"]["pending"], 1)
        self.assertEqual(report["summary"]["findings"], len(report["findings"]))
        for finding in report["findings"]:
            self.assertIn(finding["severity"], ("error", "warning"))
            self.assertIsInstance(finding["message"], str)

    def test_analyzer_is_deterministic_and_does_not_mutate_events(self):
        events = [call("a", arguments={"q": [1, True]}), call("b"), result("b")]
        saved = deepcopy(events)
        self.assertEqual(analyze_trace(events), analyze_trace(events))
        self.assertEqual(events, saved)


class RepetitionTests(unittest.TestCase):
    def repetition(self, arguments, **options):
        events = []
        for index, args in enumerate(arguments):
            events += [call(str(index), arguments=args), result(str(index))]
        return analyze_trace(events, **options)

    def test_repetition_starts_at_configured_threshold(self):
        self.assertNotIn("repeated_call", codes(self.repetition([{}, {}])))
        report = self.repetition([{}, {}, {}])
        self.assertIn("repeated_call", codes(report))
        repeated = next(f for f in report["findings"] if f["code"] == "repeated_call")
        self.assertEqual(repeated["call_ids"], ["0", "1", "2"])
        self.assertEqual(repeated["severity"], "warning")
        self.assertNotIn("repeated_call", codes(self.repetition([{}, {}, {}], repeat_threshold=4)))

    def test_object_key_order_is_irrelevant_but_array_order_matters(self):
        report = self.repetition([{"q": 1, "options": {"x": 2, "y": 3}},
                                  {"options": {"y": 3, "x": 2}, "q": 1}], repeat_threshold=2)
        self.assertIn("repeated_call", codes(report))
        self.assertNotIn("repeated_call", codes(self.repetition([[1, 2], [2, 1]], repeat_threshold=2)))

    def test_json_booleans_do_not_equal_numbers_even_nested(self):
        for args in ([True, 1], [{"enabled": True}, {"enabled": 1}], [[False], [0]]):
            with self.subTest(args=args):
                self.assertNotIn("repeated_call", codes(self.repetition(args, repeat_threshold=2)))

    def test_equal_json_numbers_compare_equal(self):
        self.assertIn("repeated_call", codes(self.repetition([1, 1.0], repeat_threshold=2)))

    def test_large_equal_json_numbers_have_equal_signatures(self):
        report = self.repetition([{"n": 10 ** 100}, {"n": 1e100}], repeat_threshold=2)
        self.assertIn("repeated_call", codes(report))

    def test_different_tools_with_equal_arguments_are_not_repetition(self):
        events = [call("a", tool="search"), call("b", tool="read"), result("a"), result("b")]
        self.assertNotIn("repeated_call", codes(analyze_trace(events, repeat_threshold=2)))

    def test_identical_call_after_observed_failure_is_retry(self):
        events = [call("a"), result("a", status="error"), call("b"), result("b")]
        report = analyze_trace(events, max_errors=1)
        retry = next(f for f in report["findings"] if f["code"] == "error_retry")
        self.assertEqual(retry["call_ids"], ["b"])

    def test_concurrent_calls_are_not_inferred_to_be_retries(self):
        events = [call("a"), call("b"), result("a", status="error"), result("b")]
        report = analyze_trace(events, max_errors=1)
        self.assertNotIn("error_retry", codes(report))

    def test_timestamps_can_establish_retry_despite_event_order(self):
        events = [call("a", timestamp=0), call("b", timestamp=30),
                  result("b", timestamp=40), result("a", status="error", timestamp=20)]
        self.assertIn("error_retry", codes(analyze_trace(events, max_errors=1)))

    def test_equal_timestamps_alone_do_not_establish_retry(self):
        events = [call("a", timestamp=0), call("b", timestamp=20),
                  result("a", status="error", timestamp=20), result("b", timestamp=30)]
        self.assertNotIn("error_retry", codes(analyze_trace(events, max_errors=1)))

    def test_known_timestamps_override_reordered_events_for_retry_inference(self):
        events = [call("a", timestamp=0), result("a", status="error", timestamp=30),
                  call("b", timestamp=10), result("b", timestamp=20)]
        self.assertNotIn("error_retry", codes(analyze_trace(events, max_errors=1)))

    def test_call_never_counts_its_own_out_of_order_error_as_a_retry(self):
        events = [result("a", status="error"), call("a")]
        self.assertNotIn("error_retry", codes(analyze_trace(events, max_errors=1)))


class TimingAndBudgetTests(unittest.TestCase):
    def test_lossy_integer_timestamps_are_rejected_before_duration_or_order_checks(self):
        base = 2 ** 53
        for start, end in ((base, base + 1), (base + 1, base)):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(
                    TraceError, r"timestamp_ms: number exceeds supported round-trip precision"):
                analyze_trace([call(timestamp=start), result(timestamp=end)], max_duration_ms=0)

    def test_timestamp_conversion_requires_binary_and_decimal_round_trip(self):
        # The first integer converts exactly in binary but its shortest float
        # text differs; the second has matching text but changes value in binary.
        for timestamp in (36028797018963968, 36028797018963970):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                    TraceError, r"timestamp_ms: number exceeds supported round-trip precision"):
                analyze_trace([call(timestamp=timestamp), result(timestamp=timestamp)])

    def test_large_exact_timestamps_preserve_duration_and_budget_findings(self):
        base = 2 ** 53
        report = analyze_trace([call(timestamp=base), result(timestamp=base + 2)],
                               max_duration_ms=1)
        self.assertEqual(report["summary"]["duration_ms"], 2)
        self.assertEqual(report["calls"][0]["duration_ms"], 2)
        self.assertIn("duration_budget", codes(report))
        with self.assertRaisesRegex(TraceError, "result timestamp precedes"):
            analyze_trace([call(timestamp=base + 2), result(timestamp=base)])

    def test_finite_floating_timestamps_retain_finite_durations(self):
        for start, end in ((0.1, 0.3), (0.0, float.fromhex("0x1.fffffffffffffp+1023"))):
            with self.subTest(start=start, end=end):
                report = analyze_trace([call(timestamp=start), result(timestamp=end)])
                self.assertTrue(math.isfinite(report["summary"]["duration_ms"]))
                self.assertEqual(report["summary"]["duration_ms"], end - start)
                self.assertEqual(report["calls"][0]["duration_ms"], end - start)

    def test_budget_boundary_is_allowed_and_excess_is_error(self):
        events = [call(timestamp=0), result(status="error", timestamp=10)]
        report = analyze_trace(events, max_calls=1, max_errors=1, max_duration_ms=10)
        self.assertFalse(any(f["severity"] == "error" for f in report["findings"]))
        report = analyze_trace(events, max_calls=0, max_errors=0, max_duration_ms=9)
        for code in ("call_budget", "error_budget", "duration_budget"):
            self.assertTrue(any(f["code"] == code and f["severity"] == "error" for f in report["findings"]))

    def test_no_timestamps_does_not_invent_duration(self):
        report = analyze_trace([call(), result()], max_duration_ms=100)
        self.assertIsNone(report["summary"]["duration_ms"])
        self.assertIn("duration_unavailable", codes(report))
        self.assertNotIn("duration_budget", codes(report))

    def test_one_timestamp_is_insufficient_to_compute_span(self):
        report = analyze_trace([call(timestamp=10), result()])
        self.assertIsNone(report["summary"]["duration_ms"])
        self.assertIsNone(report["calls"][0]["duration_ms"])
        self.assertIn("partial_timing", codes(report))

    def test_partial_timing_is_explained_and_uses_observed_span(self):
        report = analyze_trace([call("a", timestamp=10), call("b"),
                                result("a", timestamp=35), result("b")])
        self.assertEqual(report["summary"]["duration_ms"], 25)
        self.assertIn("partial_timing", codes(report))
        self.assertIsNone(report["calls"][1]["duration_ms"])

    def test_zero_duration_is_valid(self):
        report = analyze_trace([call(timestamp=0), result(timestamp=0)], max_duration_ms=0)
        self.assertEqual(report["summary"]["duration_ms"], 0)
        self.assertEqual(report["calls"][0]["duration_ms"], 0)
        self.assertNotIn("duration_budget", codes(report))

    def test_negative_matched_call_duration_is_invalid(self):
        with self.assertRaises(TraceError):
            analyze_trace([call(timestamp=20), result(timestamp=10)])


class ValidationTests(unittest.TestCase):
    def test_unknown_event_type_and_invalid_containers_are_rejected(self):
        for events in (None, {}, "events", [None], [{"type": "message", "id": "a"}]):
            with self.subTest(events=events), self.assertRaises(TraceError):
                analyze_trace(events)

    def test_additional_json_fields_are_accepted(self):
        event = call()
        event["metadata"] = {"source": "framework", "attempt": 1}
        self.assertEqual(analyze_trace([event, result()])["findings"], [])

    def test_duplicate_call_or_result_ids_are_rejected(self):
        for events in ([call(), call()], [result(), result()]):
            with self.subTest(events=events), self.assertRaises(TraceError):
                analyze_trace(events)

    def test_ids_and_tool_names_must_be_nonempty_strings(self):
        for bad in (None, "", 1, False, []):
            for key in ("id", "tool"):
                event = call()
                event[key] = bad
                with self.subTest(key=key, bad=bad), self.assertRaises(TraceError):
                    analyze_trace([event])

    def test_status_is_required_and_limited_to_ok_or_error(self):
        for bad in (None, "pending", True, 1):
            event = result()
            event["status"] = bad
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                analyze_trace([event])

    def test_timestamps_are_finite_nonnegative_numbers_not_booleans(self):
        for bad in (-1, math.inf, -math.inf, math.nan, True, "1", None):
            event = call()
            event["timestamp_ms"] = bad
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                analyze_trace([event])

    def test_nested_nonjson_values_are_rejected(self):
        for bad in (math.nan, math.inf, {1: "numeric key"}, (1, 2), {1, 2}, object()):
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                analyze_trace([call(arguments={"nested": [bad]})])

    def test_nonfinite_unknown_metadata_is_also_rejected(self):
        event = call()
        event["metadata"] = {"cost": math.nan}
        with self.assertRaises(TraceError):
            analyze_trace([event])

    def test_circular_arguments_raise_trace_error(self):
        value = []
        value.append(value)
        with self.assertRaises(TraceError):
            analyze_trace([call(arguments=value)])

    def test_integer_options_validate_type_and_range(self):
        for option in ("max_calls", "max_errors", "repeat_threshold"):
            for bad in (-1, True, 1.5, "3", None):
                with self.subTest(option=option, bad=bad), self.assertRaises(TraceError):
                    analyze_trace([], **{option: bad})
        for value in (0, 1):
            with self.subTest(repeat_threshold=value), self.assertRaises(TraceError):
                analyze_trace([], repeat_threshold=value)

    def test_duration_budget_is_finite_nonnegative_number_or_none(self):
        for bad in (-1, True, math.inf, math.nan, "10"):
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                analyze_trace([], max_duration_ms=bad)


if __name__ == "__main__":
    unittest.main()
