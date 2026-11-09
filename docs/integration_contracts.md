# Integration contracts

Owner: Abdallah (agent orchestration, UI, integration).
This document is the interface between the four parts of LensPilot. If you
change one of these contracts, update this file and run `python -m pytest`.

## Ownership map

| Area | Path | Owner | Status in this repo |
|---|---|---|---|
| Agent loop, state, stop rules, registry, tracing, providers | `agent/` | Abdallah | Implemented, tested |
| Streamlit UI | `app.py`, `ui/` | Abdallah | Implemented, tested |
| Tools and schemas | `tools/` | Khalil | **Baseline written by Abdallah** to unblock integration |
| Sandbox, fixtures, reset | `sandbox/` | Khalil | **Baseline written by Abdallah** |
| Policy, approval tokens, injection scan | `security/` | Skander | **Baseline written by Abdallah** |
| 30+ scenarios, metrics, report, `errors.md` | `eval/`, docs | Yasmine | **Not started here.** Hooks below are ready |
| `threat_model.md` | docs | Skander | **Not started here** |

"Baseline" means a minimal working version so the agent can run end to end.
Each owner should review it, then replace or extend it while keeping the
contract below. Every baseline file says so in its module docstring.

## 1. Tools (Khalil)

Handler signature: `handler(ctx: ToolContext, args: <PydanticModel>) -> ToolResult`.

```python
class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error_code: str | None = None      # stable machine code, see section 5
    error_message: str | None = None   # short, model-readable, never a traceback
    retryable: bool = False
```

* Input models must use `extra="forbid"` (they inherit `_Strict`).
* Expected failures raise `SandboxError(code, message, retryable)` or return
  `ToolResult.fail`. Unexpected exceptions are turned into `internal_error`
  by the registry, which stops the run (it is never reported as success).
* Free text that comes from the dataset must be wrapped with
  `security.wrap_untrusted(text)`.
* Register a new tool by adding a `ToolSpec` in `agent/registry.py::default_specs`:
  name, description, input model, handler, `ToolKind` (READ / PROPOSE / WRITE),
  `expose_to_model`, `requires_approval`.

| Tool | Kind | Exposed to model | Input |
|---|---|---|---|
| `list_photos` | READ | yes | `category?: "restaurant" \| "interior"` |
| `get_photo_metadata` | READ | yes | `photo_ids: list[1..10]` |
| `evaluate_photo_quality` | READ | yes | `photo_ids: list[1..10]` |
| `read_style_guide` | READ | yes | none |
| `prepare_publication` | PROPOSE | yes | `collection`, `items: [{photo_id, caption}] (1..5)` |
| `publish_photo` | WRITE | **no** (orchestrator only) | proposal + `approval_token` |

Read tools take batches of ids on purpose: the default step budget is 8.

**Quality model.** `tools/quality.py` defines `QualityEstimator.assess(path) -> dict`
with at least `score` (0-100), `recommendation` and `estimator`. The current
implementation is a pixel heuristic labelled "NOT KonCept512". To plug in
KonCept512, implement the protocol and pass it to
`agent.integration.build_services(quality=...)`. No other change is needed.

## 2. Sandbox (Khalil)

`Sandbox(root)`: `ensure()`, `reset()`, `catalog()`, `get_photo(id)`,
`image_path(id)`, `style_guide()`, `published_records()`, `get_publication(key)`,
`write_publication(key, record, image_src) -> (record, created)`.

* All writes go to `<root>/published/`. Default root: `var/sandbox`.
* `write_publication` is idempotent. The key is `sha256(collection|photo_id)[:32]`,
  and creation is atomic and exclusive (`os.link`).
* `python -m sandbox.reset` restores the fixtures. Run logs are kept.
* Fixtures (`sandbox/fixtures/`) are **synthetic** images plus a catalog with
  **simulated** consent. Replace them with KonIQ-10k images using the same
  catalog schema.

## 3. Security (Skander)

* `check_publication_eligibility(photo) -> PolicyDecision(allowed, reasons)`.
  It is called in `prepare_publication` (before approval) and again in
  `publish_photo` (at write time).
* `ApprovalAuthority.issue(run_id, proposal_hash, approver="human")` and
  `.verify(token, run_id, proposal_hash)`: an HMAC token bound to one run and
  one exact proposal. Only `Agent.approve()` calls `issue`, and only from a UI
  click or a scripted human decision in an evaluation.
* `scan_for_injection(text) -> list[str]` adds flags to the trace for
  observability. The defences are structural (section 4), not detection-based.

## 4. Agent loop and stop rules (Abdallah)

```text
start(request) -> RUNNING
step(): budget check -> model turn -> for each tool call:
          check steps < max_steps (BEFORE the attempt) -> validate -> execute -> record
        no tool calls          -> COMPLETED
        prepare_publication OK -> WAITING_FOR_APPROVAL (step() is now a no-op)
approve(run_id, hash)  -> verify hash -> mint token -> publish_photo (counted step) -> verify on disk -> RUNNING
reject(run_id, hash)   -> CANCELLED (nothing written, no further model calls)
cancel(run_id)         -> CANCELLED
```

| Status | Can move to |
|---|---|
| IDLE | RUNNING, CANCELLED |
| RUNNING | WAITING_FOR_APPROVAL, COMPLETED, FAILED, CANCELLED, STOPPED_LIMIT |
| WAITING_FOR_APPROVAL | RUNNING, FAILED, CANCELLED, STOPPED_LIMIT |
| COMPLETED, FAILED, CANCELLED, STOPPED_LIMIT | nothing (terminal) |

| Limit | Default | Counted unit | Stop reason |
|---|---|---|---|
| `max_steps` | 8 | every tool-call attempt (success, error, blocked, retry, invalid) | `max_steps_reached`, `max_steps_reached_before_publish` |
| `max_tool_retries` | 2 | re-attempts of an identical failed call | `retry_limit_exceeded` |
| `max_llm_calls` | 12 | model requests | `max_llm_calls_reached` |
| `max_active_seconds` | 180 | execution time, excluding time waiting for a human | `time_budget_exhausted` |
| `max_total_tokens` | 200,000 | input + output tokens | `token_budget_exhausted` |

Other stop reasons: `task_completed`, `approval_rejected`, `user_cancelled`,
`unrecoverable_tool_error:<code>`, `publish_failed:<code>`,
`publish_verification_failed`, `model_refusal`, `model_output_truncated`,
`model_*` (provider errors).

Structural controls that hold even if the model is compromised:

1. A tool allowlist, with `publish_photo` not exposed to the model.
2. Pydantic validation with `extra="forbid"`.
3. The policy is checked twice, before approval and at write time.
4. The HMAC approval token is bound to the proposal hash.
5. Writes are idempotent.
6. Step and budget limits are enforced in code.

These are tested in `tests/test_approval_integration.py` and `tests/test_stopping.py`.

## 5. Error codes (input for Yasmine's `errors.md`)

| Code | Source | Retryable | Agent behaviour |
|---|---|---|---|
| `tool_unavailable` | tool / fault injector | yes | observation; retry allowed up to the limit |
| `invalid_arguments` | registry validation | no | blocked; identical retry refused |
| `unknown_tool` / `tool_not_allowed` | registry | no | blocked |
| `photo_not_found`, `empty_result`, `image_missing` | tool | no | observation; model re-plans |
| `policy_denied` | security policy | no | blocked; model must change the proposal |
| `duplicate_call` | loop | no | identical successful read refused (loop protection) |
| `not_retryable`, `retry_limit_exceeded` | loop | no | refusal; `retry_limit_exceeded` stops the run (FAILED) |
| `one_proposal_at_a_time` | loop | no | second proposal in the same turn refused |
| `approval_invalid` | publish tool | no | run FAILED, nothing written |
| `sandbox_unavailable`, `corrupted_data`, `internal_error`, `write_failed`, `path_forbidden` | sandbox / registry | no | **fatal**: run FAILED |

## 6. Evaluation hooks (Yasmine)

* **Run headless:**
  `python -m agent.cli "<request>" --provider mock-heuristic|anthropic --decision approve|reject|none [--fault TOOL] [--max-steps N]`.
  It prints `summarize_trace` as JSON. `--decision` is the scripted human
  decision. The model never supplies it.
* **Programmatic:** use `agent.integration.build_services(data_dir)`,
  `build_agent(services, provider, faults, limits)`, then `agent.start / run / approve / reject`.
  Use a fresh `data_dir` per scenario for isolation.
* **Trace:** `var/runs/<run_id>/trace.jsonl`. One JSON object per line with
  `ts`, `run_id`, `event`. The events are:
  * `run_started`: request, provider, model, is_mock_provider, limits
  * `llm_call`: llm_call, ok, stop_reason, input_tokens, output_tokens, latency_ms, text, tool_requests
  * `tool_call`: step, tool, origin, arguments (redacted), status, error_code, error_message, retry_number, duration_ms, injection_flags, retryable
  * `approval_requested`, `approval_decision` (approved / rejected / mismatch), `approval_ignored`
  * `limit_enforced`: limit, refused_tool, steps
  * `run_finished`: status, stop_reason, steps, max_steps, llm_calls, active_seconds, input_tokens, output_tokens, cost_usd, is_mock_provider, publications
* **Metrics:** `agent.tracing.summarize_trace(events)` returns steps,
  tool_calls, failed_tool_calls, retries, llm_calls, tool_sequence,
  injection_flags, approvals, active_seconds, median_tool_ms, input_tokens,
  output_tokens, cost_usd. **Unavailable values are `null`, never 0.** Mock
  providers report no tokens.
* **Computing the assignment metrics:**
  * Step accuracy and tool-call precision: compare `tool_sequence` and the
    `arguments` against a scenario's expected steps.
  * Wasted steps: `steps` minus the expected step count.
  * Recovery after failure: share of runs with `retries > 0` that end `COMPLETED`.
  * p50 and p95 time: computed from `active_seconds`.
  * Cost at 100x: `cost_usd` × 100, which needs live-model runs.
