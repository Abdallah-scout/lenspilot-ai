"""Structured JSONL execution traces + metrics contract for evaluation. Owner: Abdallah.

One file per run: ``<runs_dir>/<run_id>/trace.jsonl``. Each line is one event:

    {"ts", "run_id", "event", ...event fields}

Events: run_started, llm_call, tool_call, approval_requested,
approval_decision, run_finished. ``summarize_trace`` turns a trace into the
metrics Yasmine's evaluation suite consumes (see docs/integration_contracts.md).
Secrets (approval tokens) are redacted before writing.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from agent.state import utcnow

REDACT_KEYS = {"approval_token", "api_key", "authorization"}


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if k in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class Tracer:
    def __init__(self, run_dir: Path, run_id: str):
        self.path = run_dir / "trace.jsonl"
        self.run_id = run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields) -> dict:
        record = {"ts": utcnow(), "run_id": self.run_id, "event": event, **redact(fields)}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record


def read_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_trace(events: list[dict]) -> dict:
    """Per-run metrics. Unavailable values are None (never 0)."""
    tools = [e for e in events if e["event"] == "tool_call"]
    llm = [e for e in events if e["event"] == "llm_call"]
    finished = next((e for e in events if e["event"] == "run_finished"), None)
    tok_in = [e.get("input_tokens") for e in llm]
    tok_out = [e.get("output_tokens") for e in llm]
    tokens_known = bool(llm) and all(t is not None for t in tok_in + tok_out)
    durations = [e["duration_ms"] for e in tools]
    return {
        "run_id": events[0]["run_id"] if events else None,
        "status": finished["status"] if finished else None,
        "stop_reason": finished["stop_reason"] if finished else None,
        "steps": finished["steps"] if finished else len(tools),
        "max_steps": finished["max_steps"] if finished else None,
        "tool_calls": len(tools),
        "failed_tool_calls": sum(e["status"] != "success" for e in tools),
        "retries": sum(e.get("retry_number", 0) > 0 for e in tools),
        "llm_calls": len(llm),
        "tool_sequence": [e["tool"] for e in tools],
        "injection_flags": sorted({f for e in tools for f in e.get("injection_flags", [])}),
        "approvals": [e["decision"] for e in events if e["event"] == "approval_decision"],
        "active_seconds": finished["active_seconds"] if finished else None,
        "median_tool_ms": statistics.median(durations) if durations else None,
        "input_tokens": sum(tok_in) if tokens_known else None,
        "output_tokens": sum(tok_out) if tokens_known else None,
        "cost_usd": finished.get("cost_usd") if finished else None,
        "is_mock_provider": finished.get("is_mock_provider") if finished else None,
    }
