"""Durable per-run state storage (JSON, atomic replace). Owner: Abdallah."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from agent.state import AgentState

RUN_ID_RE = re.compile(r"^run_[0-9a-f]{12}$")


class RunStore:
    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"Invalid run id {run_id!r}")
        return self.runs_dir / run_id

    def save(self, state: AgentState) -> None:
        d = self.run_dir(state.run_id)
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(state.model_dump_json(indent=1))
        os.replace(tmp, d / "state.json")  # atomic: readers never see a half-written file

    def load(self, run_id: str) -> AgentState:
        path = self.run_dir(run_id) / "state.json"
        return AgentState.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 50) -> list[AgentState]:
        paths = sorted(self.runs_dir.glob("run_*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in paths[:limit]:
            try:
                out.append(AgentState.model_validate_json(p.read_text(encoding="utf-8")))
            except ValueError:
                continue  # unreadable legacy file: skip rather than crash the dashboard
        return out
