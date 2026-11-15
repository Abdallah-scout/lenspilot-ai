# LensPilot AI

An autonomous photography-portfolio publishing agent (CS496 AI Engineering,
Project 3, "The Operator"). It works through these steps:

1. A photographer asks in plain language.
2. An LLM agent lists the shoot, reads the metadata, scores quality, reads
   the studio style guide and proposes a publication.
3. A human approves or rejects the exact proposal.
4. Only then does the system write to an isolated, resettable sandbox.

## Setup, run, test

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # optional: enables the live Claude agent
streamlit run app.py                # UI on http://localhost:8501
python -m pytest                    # 42 offline tests, no API calls
python -m sandbox.reset             # restore the sandbox between demos
python -m agent.cli "Find my best three restaurant photos" --decision approve   # headless run
```

Environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Enables the live agent. Without it only mock providers are offered |
| `LENSPILOT_MODEL` | `claude-opus-5-5` | Model id |
| `LENSPILOT_EFFORT` | `medium` | Model effort level |
| `LENSPILOT_DATA_DIR` | `var/` | Holds the sandbox, run logs and approval secret |
| `LENSPILOT_APPROVAL_SECRET` | random per install | HMAC secret for approval tokens |

## Architecture

```text
Streamlit UI (app.py, ui/)                 agent/cli.py (headless, eval)
        │ start / step / approve / reject / cancel
        ▼
agent/loop.py  Agent ── provider.next_turn(system, messages, tools)   ← Claude (live) or labelled mock
   │  state machine (agent/state.py), limits (agent/config.py)
   │  every tool call → agent/registry.py: allowlist → Pydantic validation → handler
   │  persisted after every step → var/runs/<id>/state.json + trace.jsonl
   ▼
tools/ (read tools, prepare_publication, publish_photo)
   ├── security/ (eligibility policy, HMAC approval tokens, injection scan)
   └── sandbox/  (catalog, photos, the only writable folder: published/)
```

**The model chooses actions. The code controls permissions, validation,
state and stopping.**

* `publish_photo` is not in the model's tool list.
* The model can only call `prepare_publication`. The run then pauses in
  `WAITING_FOR_APPROVAL`, a saved state that consumes no LLM calls.
* A human click mints an approval token bound to the exact proposal hash.
* The orchestrator publishes, re-checking policy and the token, and verifies
  the files on disk.
* The model then sees the result and summarises.

**Stopping.** Every tool-call attempt counts toward `MAX_AGENT_STEPS = 8`, and
the check runs before each attempt. Retries (`MAX_TOOL_RETRIES = 2`), model
calls (12), active time (180 s) and tokens (200k) are capped too. Terminal
states can never be left. `docs/integration_contracts.md` lists every state,
limit, stop reason and error code.

## Verified results (2026-11-09, Python 3.14)

* `python -m pytest` → **42 passed**. All tests use mock providers and
  temporary sandboxes. They prove:
  * the step limit is never exceeded (limits 1, 3 and 8, plus parallel bursts);
  * retries count toward the limit;
  * cancel, reject and completed runs never resume;
  * there is no write before approval and no duplicate on repeated approval;
  * an altered proposal invalidates the approval;
  * private photos and compromised models cannot publish;
  * the UI views render real sandbox and trace data, and reruns don't duplicate writes.
* The full UI flow (start, fault and retry, approval card, approve,
  portfolio, trace) was driven in headless Chromium.
* **Not yet verified:** a live Claude run. No API key was available while
  building. The Claude request and response code is tested against the SDK's
  own types (`tests/test_providers.py`), but token and cost figures for the
  report need a live run.

## Known limitations

* The photos are **synthetic** (`sandbox/make_fixtures.py`), not KonIQ-10k,
  and the consent values are **simulated**.
* Quality comes from a labelled pixel heuristic, **not KonCept512**.
* Run state is stored in local JSON with an in-process lock. That is enough
  for one Streamlit server, but not for several server processes.
* The UI drives the agent synchronously. Cancel takes effect between model
  turns, not during a model call.

## Contributions

| Member | Responsibility | Location |
|---|---|---|
| Abdallah | Agent loop, state, stop rules, registry, tracing, providers, integration, Streamlit UI, demo | `agent/`, `ui/`, `app.py`, `tests/`, `docs/` |
| Khalil | Tools, schemas, dataset, sandbox, publication backend | `tools/`, `sandbox/`. A baseline was written by Abdallah to unblock integration and is pending Khalil's review and extension |
| Skander | Security policy, injection defences, approval policy, threat model | `security/` (baseline written by Abdallah, pending review), `threat_model.md` (to do) |
| Yasmine | 36 trajectory scenarios, benchmark harness, metrics, error taxonomy, postmortem, 4-page report, oral defense prep | `eval/`, `errors.md`, `postmortem.md`, `report.pdf`, `report.md`, `yasmine.md` (Fully implemented & verified) |

All 44 tests pass (`python -m pytest`), and all 36 evaluation scenarios pass (`python -m eval.runner`).
Still required by the assignment and owned by Skander: `threat_model.md`.

