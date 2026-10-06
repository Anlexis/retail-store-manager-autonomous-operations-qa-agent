# Store Manager Operations Q&A Agent

AI agent for answering store managers' operational questions, built with Agentic Star.

> **Category**: Cat 2 (a domain pipeline: several steps orchestrated to do one specific job)
> **Industry**: Retail
> **Template ID**: RET-C2-253

## Overview

A question-answering agent for convenience-store and supermarket managers working the floor.
It takes an operational question — a cooler making a noise, an incident that has to be logged,
a food-loss reporting deadline, a procedure a new staff member needs — classifies what kind of
question it is, reads that store's own incident history, retrieves the matching standard
operating procedure, and answers with the steps to take.

Two things make it more than a search box:

- **Escalation is decided, not left to the reader.** A fault that has already been escalated at
  this store is recurrent, and the answer says so: it names the escalation tier and the contact,
  and the response boundary refuses to release an answer that flags an escalation while omitting
  the instruction.
- **The answer is written in the language the staff member reads, and the record is not.**
  Store floors are multilingual; head-office incident records are filed in Japanese. So an
  equipment fault answered in Vietnamese still files its record in Japanese, and the original
  Japanese procedure is appended below the translation — a translation error must not be the only
  copy of a safety instruction.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails during
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling the agent

The standalone entry point exposes `POST /invoke` and `GET /health`.

```bash
curl -X POST http://localhost:8000/invoke \
  -H "Authorization: Bearer $INVOKE_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "input": "The cooler in Aisle 3 is making a loud noise again. What is the escalation procedure?",
        "input_context": {"store_id": "STORE-042", "staff_lang": "vi"}
      }'
```

- `input` — the question, as the store manager would type it. Japanese, English, Vietnamese and
  Filipino all work.
- `input_context` — optional, and a **closed set**: `store_id` and `staff_lang` only. An
  undeclared field is refused with `400` rather than ignored. Both are validated against inert
  alphabets, and a credential-shaped value is refused by name.
- `INVOKE_AUTH_TOKEN` — when set on the server, a caller with no upstream authentication must
  present it. The entry contract requires a verified external caller, so a deployment that leaves
  it unset and has no authenticating middleware in front will refuse every request.

The response carries the answer in the staff member's language, the Japanese head-office record
where one applies, the escalation flag and tier, and the question class.

## Configuration

| File | What it holds |
|---|---|
| `config/agent.yaml` | The static manifest: identity, entry point, trust level, compile-time requirements. No runtime values. |
| `config/config.yaml` | Runtime parameters, all of which have a reader: `max_retry`, `timeout_s`, and the `store_ops` block (`default_language`, `escalation_history_threshold`, `max_incident_entries`). Values are bounds-checked on load; an out-of-range or non-finite entry degrades to the documented default rather than silently changing what a store manager is told. |

## Project Structure

```
src/graph/     the outer backbone, the inner domain workflow, and the bridge between them
src/nodes/     one module per workflow step
src/services/  the caller contract, the output contract, and the runtime-config loader
src/schemas/   the state definition
src/api/       the standalone HTTP entry point
tests/         unit, integration and boundary tests
config/        the manifest and the runtime parameters
docs/          design specification and test specification
```

`docs/02_design.md` describes the architecture and the security boundaries;
`docs/03_test_spec.md` describes what is tested and how.

## Customising

1. Point `IncidentMemoryRecallNode` and `SOPKBSearchNode` at your own incident store and
   procedure knowledge base. Both are behind a single function each, keyed on the store
   identifier and the question class.
2. Replace the reference procedures, contacts and compliance checklists with your own.
3. Adjust `config/config.yaml` for your escalation policy and default language.
4. Extend the question classes in `QueryParseNode` if your operation has categories this one
   does not.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
