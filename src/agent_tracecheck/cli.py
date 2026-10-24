"""Read normalized JSONL traces locally and produce deterministic diagnostics."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import html
import json
import math
from pathlib import Path
import re
import sys

from . import __version__
from .core import TraceError, analyze_trace


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TraceError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(value):
    raise TraceError(f"Non-standard JSON value {value}")


def _float(token):
    try:
        value = float(token)
        exact = math.isfinite(value) and Decimal(token) == Decimal(str(value))
    except (ValueError, OverflowError, InvalidOperation):
        exact = False
    if not exact:
        raise TraceError('Decimal number exceeds supported round-trip precision')
    return value


def load_trace(text: str):
    events = []
    for number, line in enumerate(text.removeprefix('\ufeff').split('\n'), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float))
        except ValueError as error:
            raise TraceError(f"Line {number}: {error}") from error
    return events


def _line(value):
    return re.sub(r"[\x00-\x1f\x7f]", lambda m: repr(m.group())[1:-1], str(value))


def _md(value):
    return re.sub(r"([\\`*_|\[\]{}()#!])", r"\\\1", html.escape(_line(value), quote=False))


def render_report(report, output_format):
    if output_format == 'json':
        return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    summary = report['summary']
    counts = (f"{summary['calls']} calls, {summary['errors']} errors, "
              f"{summary['pending']} pending, {summary['findings']} findings")
    duration = summary['duration_ms']
    span = 'unavailable' if duration is None else f'{duration:g} ms'
    if output_format == 'markdown':
        lines = ['# Agent trace report', '', counts, '', f'Observed timestamp span: {span}.', '']
        if report['findings']:
            lines += ['| Level | Code | Finding |', '| --- | --- | --- |']
            for finding in report['findings']:
                lines.append('| ' + ' | '.join(_md(finding[key]) for key in (
                    'severity', 'code', 'message'
                )) + ' |')
        else:
            lines.append('No findings under the selected rules and budgets.')
        lines += ['', 'Recorded traces only; timing may be partial and repetitions may be intentional.']
    else:
        lines = [f'Agent trace report: {counts}', f'Observed timestamp span: {span}.']
        for finding in report['findings']:
            lines.append(_line(f"[{finding['severity'].upper()}] {finding['code']}: {finding['message']}"))
        if not report['findings']:
            lines.append('No findings under the selected rules and budgets.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect normalized agent tool-call JSONL traces offline.')
    parser.add_argument('trace', help='UTF-8 JSONL trace file, or - for stdin')
    parser.add_argument('--version', action='version', version=f'agent-tracecheck {__version__}')
    parser.add_argument('--max-calls', type=int, default=50)
    parser.add_argument('--max-errors', type=int, default=0)
    parser.add_argument('--max-duration-ms', type=float, default=None,
                        help='budget for observed timestamp span; partial timing can underestimate the run')
    parser.add_argument('--repeat-threshold', type=int, default=3)
    parser.add_argument('--format', choices=('text', 'json', 'markdown'), default='text')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--fail-on', choices=('error', 'warning', 'never'), default='error',
                        help='warning includes error; default: error')
    args = parser.parse_args(argv)
    try:
        trace_path = Path(args.trace) if args.trace != '-' else None
        if args.output and trace_path:
            same_path = args.output.resolve() == trace_path.resolve()
            try:
                same_file = args.output.samefile(trace_path)
            except FileNotFoundError:
                same_file = False
            if same_path or same_file:
                raise TraceError('Output must not overwrite the input trace')
        data = sys.stdin.read() if trace_path is None else trace_path.read_text(encoding='utf-8-sig')
        report = analyze_trace(
            load_trace(data), max_calls=args.max_calls, max_errors=args.max_errors,
            max_duration_ms=args.max_duration_ms, repeat_threshold=args.repeat_threshold,
        )
        output = render_report(report, args.format)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding='utf-8')
        else:
            sys.stdout.write(output)
        levels = {'error': {'error'}, 'warning': {'error', 'warning'}, 'never': set()}
        return int(any(item['severity'] in levels[args.fail_on] for item in report['findings']))
    except (TraceError, ValueError, OSError, UnicodeError, RecursionError) as error:
        print(f'agent-tracecheck: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
