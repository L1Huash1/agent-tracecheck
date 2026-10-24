"""Record real local LangChain tools; no model, API key, or hosted trace is used."""

import argparse
import asyncio
import os

# Disable optional framework tracing in this demonstration process only.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_OTEL_ENABLED"] = "false"

from langchain_core.tools import tool
from agent_tracecheck.integrations.langchain import LangChainTraceHandler


@tool
def lookup(note_id: str) -> str:
    """Look up a note in a local dictionary."""
    return {"release": "Review the local release checklist."}[note_id]


@tool
async def count_words(text: str) -> int:
    """Count the words in a local string asynchronously."""
    await asyncio.sleep(0)
    return len(text.split())


async def record(path):
    with LangChainTraceHandler(path) as handler:
        config = {"callbacks": [handler]}
        lookup.invoke({"note_id": "release"}, config=config)
        for _ in range(3):
            try:
                lookup.invoke({"note_id": "missing"}, config=config)
            except KeyError:
                pass  # Deliberately repeated failures demonstrate diagnostics.
        await asyncio.gather(*(count_words.ainvoke({"text": text}, config=config)
                               for text in ("one two", "one two three")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="new JSONL path; existing files are refused")
    args = parser.parse_args()
    asyncio.run(record(args.output))
    print("Recorded 6 local tool calls, including 3 intentional failures.")
    print("Inspect with: agent-tracecheck <your-trace-path> --fail-on never")
