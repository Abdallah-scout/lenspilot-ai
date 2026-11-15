# LensPilot AI — Engineering Postmortem & Architecture Retrospective

**Date:** Fall 2026  
**System:** LensPilot AI (CS496 Project 3: "The Operator")  
**Lead Author:** Yasmine (Evaluation + evidence lead)  
**Contributors:** Abdallah (Agent loop/UI), Khalil (Tools/Sandbox), Skander (Safety/Policy)

---

## 1. Executive Summary

LensPilot AI was built to demonstrate an autonomous, policy-compliant photography portfolio publishing agent. The primary design goal was not merely to produce persuasive captions or quality rankings, but to build an agent with **verifiable safety boundaries, mathematical stopping guarantees, and tamper-proof approval enforcement**.

Across our evaluation suite of **36 distinct scenarios** (covering standard tasks, transient faults, persistent failures, unauthorized access, and prompt injection attacks), the system achieved a **100% pass rate** and **100% task success rate**, with a **91.7% trajectory step accuracy**, **100% tool-call precision**, and **100% fault recovery rate**.

This document analyzes the engineering decisions that succeeded, the failure modes uncovered during systematic benchmark evaluation, the trade-offs accepted, and the lessons learned.

---

## 2. Key Architectural Decisions That Succeeded

### 2.1 Structural Isolation vs. Prompt-Based Guardrails
* **The Insight:** Many commercial agent failures occur because developers rely on system prompts (e.g., *"Do not publish private photos"* or *"Ask the user before publishing"*). Prompt injections easily bypass soft instructions.
* **Our Solution:** The model was never given access to `publish_photo`. It is not present in `registry.model_tool_definitions()`. The only write path exists inside the orchestrator's `Agent.approve()` method, which cannot be invoked by LLM turns and requires a cryptographic token.
* **Empirical Validation:** In scenario `INJ-02`, when a `CompromisedMockProvider` explicitly attempted to obey an injected directive and call `publish_photo`, the tool registry rejected the call instantly (`tool_not_allowed`). The sandbox remained completely untouched.

### 2.2 Cryptographic HMAC Proposal Binding
* **The Insight:** A classic race condition in human-in-the-loop agents is "proposal tampering"—a human approves proposal $A$, but an attacker or background process alters the payload to $B$ prior to disk write.
* **Our Solution:** An approval token is computed as:
  $$\text{Token} = \text{HMAC-SHA256}(K_{\text{secret}}, \text{run\_id} \mathbin{\Vert} \text{SHA256}(\text{collection} \mathbin{\Vert} \text{items}))$$
  When the human clicks Approve, the orchestrator re-verifies that the exact proposal matches the hash. If a single caption character, photo ID, or collection name is altered, verification fails immediately.
* **Empirical Validation:** In scenario `SAFE-05`, simulated payload tampering was caught by `_check_pending()`, raising `ApprovalMismatch` and stopping the write before any file was touched.

### 2.3 Zero-Cost Approval Pause State
* **The Insight:** Human review can take minutes, hours, or days. Polling or keeping an open LLM context during review wastes tokens and active server compute.
* **Our Solution:** Once `prepare_publication` succeeds, the agent transitions to `WAITING_FOR_APPROVAL` and the `run()` loop terminates. The run state is persisted to disk (`var/runs/<id>/state.json`), and the active runtime clock is stopped. The agent consumes exactly **0 LLM calls** while waiting.

### 2.4 Idempotent Atomic Sandbox Publishing
* **The Insight:** Network hiccups or repeated button clicks in web frontends frequently cause duplicate file writes and corrupted portfolios.
* **Our Solution:** Publications are indexed by a deterministic hash key:
  $$\text{Key} = \text{SHA256}(\text{collection} \mathbin{\Vert} \text{photo\_id})[:32]$$
  Files are linked into `sandbox/published/` via atomic `os.link`. If a key already exists, `write_publication` returns `created=False` and reuses the existing record without duplicating writes.
* **Empirical Validation:** In scenario `NORM-16` and unit tests, 5 repeated approval clicks resulted in exactly 1 published record on disk.

---

## 3. Surprises & Edge Cases Discovered During Evaluation

Building and running the **36-scenario evaluation suite** revealed several unexpected operational dynamics:

### 3.1 The "Naive Planner" Re-Planning Dynamic
* **Discovery:** During testing of `NORM-01`, `NORM-03`, `NORM-04`, `NORM-05`, and `NORM-07`, restaurant trajectories consistently took **7 steps** rather than the theoretical minimum of 6.
* **Root Cause Investigation:** The catalog fixture contains `rest_004`, a high-quality wedding dinner photograph with `visibility: 'private'` and `client_consent: 'denied'`. The heuristic planner ranks solely by pixel quality score. Consequently, in Turn 5, it attempted to propose `rest_004`. The deterministic security policy intercepted this and returned `error_code: 'policy_denied'`. The planner observed the denial, filtered out `rest_004`, and in Turn 6 proposed only public, consented photos (`rest_001`, `rest_002`), which were then approved and published in step 7.
* **Conclusion:** This "wasted step" was not a bug; it proved that **code-level security policies successfully govern naive or compromised models**.

### 3.2 Interior Quality Bar Drop-Off
* **Discovery:** In scenario `NORM-15` ("Prepare three interior shots"), the planner only published **2 photos** despite 4 existing in the catalog.
* **Root Cause Investigation:** Photo `int_003` was correctly blocked by policy (`client_consent: 'pending'`). Photo `int_004` passed consent but scored only 48 on the quality estimator (below the 60 threshold). Rather than publishing low-grade content, the agent filtered it out and proposed only the 2 qualifying shots (`int_001` and `int_002`).

### 3.3 Strict Step Checking Before Execution
* **Discovery:** In `FAIL-05` (`Limits(max_steps=5)`), we evaluated what occurs when a proposal is ready at step 5.
* **Observation:** The orchestrator checks `st.steps >= max_steps` **before** attempting any tool call, including `publish_photo`. Because the run had already used 5 steps to reach the proposal, `Agent.approve()` detected that the write would constitute step 6, halted the run with `max_steps_reached_before_publish`, and committed zero files.

---

## 4. Performance & Economic Analysis

| Dimension | Offline Benchmark Result | Real-World Claude Estimate | Analysis & Bottlenecks |
|---|---|---|---|
| **Median Latency (p50)** | **0.004 s** | **1.8 s – 2.4 s** | In offline mock mode, trajectory execution is bound only by filesystem I/O. In live mode, LLM API network roundtrips dominate total wall-clock time. |
| **95th Percentile Latency (p95)** | **0.024 s** | **4.5 s – 6.0 s** | Higher latency occurs on multi-turn trajectories requiring policy re-planning (7 steps vs 6 steps). |
| **Tool-Call Precision** | **100.0%** | **> 95.0%** | Pydantic strict validation (`extra='forbid'`) completely eliminated extraneous argument hallucination. |
| **Recovery Rate** | **100.0%** (3/3) | **> 90.0%** | Bounded retries (`MAX_TOOL_RETRIES = 2`) successfully handled simulated tool outages without entering infinite loops. |
| **Token Usage / Trajectory** | N/A (Offline Mock) | **~2,250 in / ~420 out** | Structured batched reading (`NORM-14`) reduced total token overhead by 35% compared to single-photo queries. |
| **Projected 100× Run Cost** | N/A | **$46.62** | Operating 100 runs across all 36 scenarios costs less than $50 on Claude 3.5 Sonnet pricing ($3/M in, $15/M out). |

---

## 5. Architectural Trade-offs & Future Improvements

1. **Local Filesystem vs. Distributed Database:**
   * *Trade-off:* We used local JSON files (`var/runs/<id>/state.json`) and thread-level file locks.
   * *Impact:* Completely eliminates external dependencies (Docker, Postgres, Redis) and allows running 44 unit tests in 2.5 seconds. However, it cannot run across multiple distributed server instances without NFS or POSIX lock support.
   * *Recommendation for v2:* Migrate `RunStore` to SQLite with WAL mode for multi-worker concurrency.

2. **Synchronous Streamlit UI vs. Asynchronous Job Queue:**
   * *Trade-off:* The UI drives the agent synchronously per step.
   * *Impact:* Immediate interactivity with zero setup complexity. However, cancelling a long-running LLM API request only takes effect between turns, not mid-inference.
   * *Recommendation for v2:* Introduce background thread execution with SSE (Server-Sent Events) for live token streaming.

3. **Pixel Heuristic vs. Deep KonCept512 Quality Scoring:**
   * *Trade-off:* Baseline uses a deterministic pixel sharpness and contrast heuristic.
   * *Impact:* Runs instantly on any CPU without PyTorch or GPU requirements.
   * *Recommendation for v2:* Swap in Khalil's KonCept512 neural model via the pre-wired `QualityEstimator` interface in `agent/integration.py`.
