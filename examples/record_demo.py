"""Record scripted local tool calls with two simulated failures and a retry.

This runs an actual local dictionary lookup. The temporary failures are scripted
to demonstrate trace findings; this is not an LLM agent run or an agent-quality
benchmark. No model, external service, network, or subprocess is used.

After installing agent-tracecheck, run:
    python examples/record_demo.py path/to/new-demo.jsonl

The output file must not already exist. Arguments are recorded; redact sensitive
data before using TraceRecorder in your own application.
"""

import argparse
from pathlib import Path

from agent_tracecheck.recording import TraceRecorder


def record_demo(path: Path) -> dict:
    """Write a six-event trace and return the final local lookup result."""
    catalog = {"agent-toolkit": {"name": "Agent Toolkit", "available": True}}
    attempts = 0

    def lookup(query: str) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise LookupError("scripted temporary failure for the recording example")
        return catalog[query]

    with TraceRecorder(path) as recorder:
        for _ in range(3):
            try:
                return recorder.call("catalog_lookup", {"query": "agent-toolkit"},
                                     lambda: lookup("agent-toolkit"))
            except LookupError:
                if attempts == 3:
                    raise
    raise RuntimeError("demo did not complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record scripted local calls; not an LLM benchmark.")
    parser.add_argument("output", type=Path, help="new JSONL file (existing files are never overwritten)")
    args = parser.parse_args()
    result = record_demo(args.output)
    print(f"Recorded 3 scripted local calls (2 errors, then success) to {args.output}")
    print(f"Final catalog item: {result['name']}")
