"""Optional LangSmith tracing. Off unless the user turns it on.

On only when ALL of these hold:
  * LENSPILOT_LANGSMITH=1            (explicit project opt-in)
  * LANGSMITH_API_KEY is set
  * the code is not running under pytest
Otherwise tracing is forced off, so a LANGSMITH_TRACING variable left over
from another project cannot upload this project's runs.

What leaves the PC: node and tool names, status, counts, timings, token usage,
model name, run id. Inputs and outputs (request, photo data, captions) are
hidden by default (LANGSMITH_HIDE_INPUTS / LANGSMITH_HIDE_OUTPUTS = true), and
the graph state never contains them in the first place (see agent/graph.py).
The local trace.jsonl is unaffected and stays the source for the evaluation.

Import this module before langgraph: it sets the variables langsmith reads.
"""

from __future__ import annotations

import os
import sys
from contextlib import nullcontext

import agent.config  # noqa: F401  (loads .env first)

_FLAGS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2")


def enabled() -> bool:
    return (os.environ.get("LENSPILOT_LANGSMITH") == "1" and bool(os.environ.get("LANGSMITH_API_KEY"))
            and "pytest" not in sys.modules)


def configure() -> bool:
    on = enabled()
    for flag in _FLAGS:
        os.environ[flag] = "true" if on else "false"
    if on:
        os.environ.setdefault("LANGSMITH_PROJECT", "lenspilot")
        os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
        os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
    return on


ON = configure()


def span(name: str, run_type: str, **metadata):
    """Child span with metadata only (no inputs/outputs). Does nothing when tracing is off."""
    if not ON:
        return nullcontext()
    from langsmith import trace
    return trace(name, run_type=run_type, metadata=metadata)


def annotate(run, **metadata) -> None:
    """Attach result metadata (status, tokens...) to an open span."""
    if run is not None:
        run.metadata.update({k: v for k, v in metadata.items() if v is not None})
