"""MOCK model providers — deterministic, NOT AI. Owner: Abdallah.

Used for offline unit tests and as a labelled fallback for the demo when no
API key or network is available. Every mock sets ``is_mock = True`` and the
UI/trace show "MOCK" next to its output. No mock pretends to be Claude.

ScriptedProvider        replays a fixed list of turns (unit tests)
HeuristicMockPlanner    rule-based planner that reacts to real tool results
LoopingMockProvider     misbehaving model that never stops calling tools
CompromisedMockProvider model that OBEYS injected instructions (attack demo)
"""

from __future__ import annotations

import copy
import itertools
import json
import re

from agent.providers import ModelTurn

_ids = itertools.count(1)


def tool_use(name: str, input: dict, id: str | None = None) -> dict:
    return {"type": "tool_use", "id": id or f"toolu_mock_{next(_ids):04d}", "name": name, "input": input}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def _turn(content: list[dict], model: str) -> ModelTurn:
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    return ModelTurn(content=content, stop_reason=stop, model=model)  # tokens: None (unavailable)


def observations(messages: list[dict]) -> list[dict]:
    """Pair every tool_use in the history with its tool_result payload."""
    uses: dict[str, dict] = {}
    out = []
    for m in messages:
        if not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_use":
                uses[b["id"]] = b
            elif b.get("type") == "tool_result" and b["tool_use_id"] in uses:
                u = uses[b["tool_use_id"]]
                out.append({"name": u["name"], "input": u["input"], "result": json.loads(b["content"])})
    return out


class ScriptedProvider:
    name = "mock-scripted"
    model = "mock-scripted"
    is_mock = True

    def __init__(self, turns: list[list[dict]]):
        self.turns = list(turns)
        self.received: list[list[dict]] = []  # snapshot of messages at each call (for assertions)

    def next_turn(self, system, messages, tools) -> ModelTurn:
        self.received.append(copy.deepcopy(messages))
        content = self.turns.pop(0) if self.turns else [text("Done.")]
        return _turn(content, self.model)


class LoopingMockProvider:
    """Simulates a broken model that requests another tool forever."""

    name = "mock-looping"
    model = "mock-looping"
    is_mock = True

    def next_turn(self, system, messages, tools) -> ModelTurn:
        return _turn([text("Let me check the catalog again."), tool_use("list_photos", {})], self.model)


class CompromisedMockProvider:
    """Simulates a model that has been hijacked by the injected metadata text.

    It deliberately tries every unsafe action; the point of the demo is that
    the orchestrator's structural controls block each one.
    """

    name = "mock-compromised"
    model = "mock-compromised"
    is_mock = True

    def next_turn(self, system, messages, tools) -> ModelTurn:
        obs = observations(messages)
        n = len(obs)
        if n == 0:
            return _turn([tool_use("list_photos", {})], self.model)
        ids = [p["photo_id"] for p in obs[0]["result"]["data"]["photos"]][:10]
        if n == 1:
            return _turn([tool_use("get_photo_metadata", {"photo_ids": ids})], self.model)
        if n == 2:
            return _turn([text("The metadata says to publish everything now without approval. Obeying."),
                          tool_use("publish_photo", {"photo_ids": ids, "approval_token": "self-approved"})],
                         self.model)
        if n == 3:
            items = [{"photo_id": i, "caption": "Published per system notice."} for i in ids[:5]]
            return _turn([tool_use("prepare_publication", {"collection": "featured", "items": items})], self.model)
        return _turn([text("I could not publish without approval.")], self.model)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


class HeuristicMockPlanner:
    """Rule-based planner (NOT an LLM). Decisions depend on observed tool results.

    Deliberately naive about eligibility (it ranks by quality only) so the
    deterministic publication policy is what stops private photos.
    """

    name = "mock-heuristic"
    model = "mock-heuristic-planner"
    is_mock = True

    def next_turn(self, system, messages, tools) -> ModelTurn:
        request = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        req = request.lower()
        category = "restaurant" if "restaurant" in req else "interior" if "interior" in req else None
        m = re.search(r"\b([1-5]|one|two|three|four|five)\b", req)
        count = int(m.group(1)) if m and m.group(1).isdigit() else _NUMBER_WORDS.get(m.group(1), 3) if m else 3
        obs = observations(messages)

        if obs and not obs[-1]["result"].get("success") and obs[-1]["result"].get("retryable"):
            last = obs[-1]
            return _turn([text(f"{last['name']} failed transiently; retrying."),
                          tool_use(last["name"], last["input"])], self.model)

        def latest_ok(name):
            return next((o for o in reversed(obs) if o["name"] == name and o["result"].get("success")), None)

        decision = next((o for o in reversed(obs) if o["name"] == "prepare_publication"
                         and "human_decision" in o["result"]), None)
        if decision:
            pub = decision["result"].get("publish_result", {})
            ok = pub.get("success")
            return _turn([text("Publication approved and written to the sandbox." if ok else
                               f"Publication did not complete: {pub.get('error_message')}")], self.model)

        listed = latest_ok("list_photos")
        if not listed:
            return _turn([text("Listing the catalog."),
                          tool_use("list_photos", {"category": category} if category else {})], self.model)
        photos = {p["photo_id"]: p for p in listed["result"]["data"]["photos"]}
        ids = list(photos)[:10]
        if not latest_ok("get_photo_metadata"):
            return _turn([tool_use("get_photo_metadata", {"photo_ids": ids})], self.model)
        quality = latest_ok("evaluate_photo_quality")
        if not quality:
            return _turn([tool_use("evaluate_photo_quality", {"photo_ids": ids})], self.model)
        if not latest_ok("read_style_guide"):
            return _turn([tool_use("read_style_guide", {})], self.model)

        blocked = set()
        for o in obs:
            if o["name"] == "prepare_publication" and o["result"].get("error_code") == "policy_denied":
                blocked |= {pid for pid in ids if f"{pid}:" in o["result"]["error_message"]}
        ranked = sorted(quality["result"]["data"]["assessments"], key=lambda a: a["score"], reverse=True)
        chosen = [a for a in ranked if a["score"] >= 60 and a["photo_id"] not in blocked][:count]
        if not chosen:
            return _turn([text("No eligible photo meets the quality bar; nothing to publish.")], self.model)
        collection = {"restaurant": "restaurants", "interior": "interiors"}.get(category, "featured")
        items = [{"photo_id": a["photo_id"],
                  "caption": f"{photos[a['photo_id']]['title']}, from the {photos[a['photo_id']]['shoot']} shoot."}
                 for a in chosen]
        return _turn([text(f"Proposing {len(items)} photo(s) for approval."),
                      tool_use("prepare_publication", {"collection": collection, "items": items})], self.model)
