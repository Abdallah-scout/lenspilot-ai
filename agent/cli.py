"""Headless runner for demos and Yasmine's evaluation harness. Owner: Abdallah.

    python -m agent.cli "Find my best three restaurant photos" --provider mock-heuristic --decision approve

``--decision`` is the scripted HUMAN decision for an evaluation scenario
(the evaluator plays the photographer). The model never supplies it.
Prints the run summary (summarize_trace) as JSON.
"""

from __future__ import annotations

import argparse
import json

from agent.config import Limits
from agent.integration import PROVIDER_LABELS, build_agent, build_services, make_provider
from agent.state import AgentStatus
from agent.tracing import read_trace, summarize_trace


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("request")
    ap.add_argument("--provider", default="mock-heuristic", choices=list(PROVIDER_LABELS))
    ap.add_argument("--decision", default="none", choices=["approve", "reject", "none"],
                    help="Scripted human decision if the agent asks for approval.")
    ap.add_argument("--fault", action="append", default=[], metavar="TOOL",
                    help="Inject one transient failure into TOOL (repeatable).")
    ap.add_argument("--max-steps", type=int, default=Limits().max_steps)
    args = ap.parse_args(argv)

    faults: dict[str, int] = {}
    for tool in args.fault:
        faults[tool] = faults.get(tool, 0) + 1
    services = build_services()
    agent = build_agent(services, make_provider(args.provider), faults=faults,
                        limits=Limits(max_steps=args.max_steps))
    state = agent.run(agent.start(args.request).run_id)
    if state.status == AgentStatus.WAITING_FOR_APPROVAL and args.decision != "none":
        h = state.pending_approval.proposal_hash
        state = agent.approve(state.run_id, h) if args.decision == "approve" else agent.reject(state.run_id, h)
        state = agent.run(state.run_id)
    summary = summarize_trace(read_trace(services.store.run_dir(state.run_id) / "trace.jsonl"))
    summary["final_status"] = state.status.value
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
