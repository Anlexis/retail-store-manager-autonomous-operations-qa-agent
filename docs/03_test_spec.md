# Test Specification — RET-C2-253 Store Manager Operations Q&A Agent

## Test strategy

The suite is layered, and the layers exist for different reasons.

- **Unit tests** call `execute()` directly, with no framework wrapper in front, so a refusal is
  proved to come from the node that owns the contract rather than from a framework gate that may
  be absent or configured off in a given deployment.
- **Integration tests** go through the real ASGI entry point and a compiled graph. Nothing in
  that layer calls a node directly. This distinction matters: a suite can be entirely green on
  node-level tests while the deployed agent cannot answer a single request, because those tests
  supply a trust level the HTTP path never produces.
- **Proof-of-boundary tests** pin structural invariants — import isolation, state safety, backbone
  order.

Every test is deterministic: no model, no network, no live retrieval backend.

Screens are probed in **both** directions throughout. A screen that refuses everything is not a
security control, it is an outage — and this domain's ordinary vocabulary ("insert into the
logbook", "act as duty manager") overlaps the shapes an unanchored screen looks for.

## Framework compliance

| TC-ID | Test | Expected result | Location |
|-------|------|----------------|----------|
| TC-01 | State is a flat TypedDict | `State(AgentState)`; no Pydantic, no dataclass | `tests/proof_of_boundary/test_state_safety.py` |
| TC-02 | No credential-shaped field in State | 0 violations | `tests/proof_of_boundary/test_state_safety.py`, `scripts/check_credentials.py` |
| TC-06 | The framework's input gate is not overridden | Class definition raises if it is | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | The framework's output gate is not overridden | Class definition raises if it is | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` declared on every node | All 8 nodes declare one | `scripts/check_trust_level.py` |
| TC-09 | A domain audit event on every `execute()` | All 8 nodes emit one | `scripts/check_audit_trace.py` |
| TC-10 | Dependencies exactly pinned | `setuptools==68.2.2`, dev extras pinned | `scripts/check_dep_pinning.py` |
| TC-11 | Class name agrees across manifest, graph and entry point | All resolve to `StoreManagerOpsQAAgent` | `tests/integration/test_manifest_identity_alignment.py` |

## Unit tests

### `tests/unit/test_agent.py` — per-step behaviour

| Group | Covers |
|---|---|
| TC-PPE-01…10 | PreProcessNode: valid question, store identifier and language hints, empty and too-short input, trust level, injection refusal, and a legitimate operations question NOT being a false positive |
| TC-QPE-01…08 | QueryParseNode: each question class, the equipment-class hint, empty question, trust level |
| TC-IMR-01…05 | IncidentMemoryRecallNode: history per question class, store scoping, trust level |
| TC-SKB-01…05 | SOPKBSearchNode: procedures, compliance items, contacts, identifiers per class, trust level |
| TC-ESY-01…08 | EscalationSynthesizeNode: tier per class and history, Japanese head-office record, no record where none is due, trust level |
| TC-MLA-01…06 | MultilingualAdaptNode: Japanese unchanged, the Japanese appendix on safety-critical classes, no appendix elsewhere, empty input, trust level |
| TC-RFN-01…06 | ResponseFormatNode: every released key, the empty-answer floor, escalation and record propagation, trust level |
| TC-PPO-01…06 | PostProcessNode: release and refusal paths, trust level |

> TC-PPO-01's fixture previously declared an escalation and carried no escalation instruction — so
> it asserted that the suppressed-instruction case PASSES the response boundary. It was
> strengthened to a releasable response, not relaxed to fit.

### `tests/unit/test_caller_contract.py` — the screens

| Test | Covers |
|---|---|
| Legitimate questions trip no screen | Eight real store-manager questions, English and Japanese, including ones containing "insert into", "act as", "drop", "select" |
| Attacks are detected | Chat-template control tokens as a class (`<\|im_start\|>`, `<\|system\|>`, `[INST]`, `<<SYS>>`, `<</SYS>>`), prompt-injection phrases, statement-shaped SQL |
| Markup splicing | A directive spliced across tags is caught after the markup strip; a control token is caught before it |
| Invisible characters | Both resolutions — dropped (a directive broken mid-word) and turned into spaces (a directive whose spaces were replaced) |
| Full-width normalisation | A full-width control token cannot evade an ASCII pattern |
| Credential floor | Every shape the framework's detector finds is also found here |
| Credential union | The assignment shapes the framework does NOT describe are kept — delegating entirely to it would make the screen narrower while looking like a tightening |
| Request context | Normalisation, the closed accepted set, a hostile field name not echoed back, size cap, non-mapping input |
| Block-set parity | `refused_for_credentials == bool(detect_credentials_in_value(context))` — the anti-drift property |

### `tests/unit/test_output_boundary.py` — the response boundary

Every violation path is measured on the **caller-visible envelope**, resolved the way the
framework resolves it, not on the node's return value alone.

| Test | Covers |
|---|---|
| Release | A conforming response, and one that neither escalates nor files a record |
| Withholding (9 parametrised paths) | Not a response; not a mapping; empty response; escalation instruction suppressed; escalation tier missing; escalation tier unknown; head-office record missing where one is due; record not in Japanese; credential shape — framework-described and local-assignment alike |
| Clearing | Presence AND emptiness of every output-bearing field: a missing key falls through to the merged state |
| Containment | The withheld envelope carries no released text, no traceback, no source paths |
| Truthiness | The replacement notice is truthy, because `formatted_output or result` re-opens on a falsy one |
| Reason labels | A closed set, never text built from the offending content |

### `tests/unit/test_runtime_config.py` — declared values

| Test | Covers |
|---|---|
| The shipped file loads | And every shipped value survives its own bounds check — a shipped value the loader rejects is a shipped value nothing uses |
| Non-finite matrix | `"NaN"`, `"Infinity"`, `"-Infinity"`, raw `float("nan")` / `inf` / `-inf`, bools, `None`, non-numerics, over- and under-magnitude |
| Per-field bounds | Each field, out of range in both directions, degrades to the documented default |
| In-range values honoured | Including the boundary values |
| Fallbacks mirror the shipped file | So an unreadable config degrades to the documented behaviour rather than to something else |

## Integration tests

### `tests/integration/test_invoke_contract.py` — the real entry point

| Test | Covers |
|---|---|
| Health | The service answers |
| Unauthenticated request refused | 401, and the body does not say which way the credential was wrong |
| Authenticated request answered | A real domain answer, and the full backbone including the response boundary |
| Japanese fault report | Classified as an equipment fault, escalates, files the Japanese record |
| Store scope changes the tier | Two requests differing only by store scope produce different escalation tiers |
| Staff language changes the answer | Adapted text, translated warning, Japanese appendix |
| A different question class | Compliance: no escalation, no record |
| Undeclared context field | Refused by name |
| Credential-shaped context value | Refused readably; the value is never echoed |
| Ordinary domain text on the same field | Still passes — the other direction |
| Out-of-range identifier, unsupported language | Refused |
| Hostile questions (5 parametrised) | Error status, nothing released — behavioural, not pinned to a gate's wording |
| Over-long and too-short questions | Refused |
| A refusal is not papered over | The workflow cannot turn a refused request into an answer |
| The released envelope | Scanned whole for credential shapes |
| Escalating response | Carries the escalation instruction end to end |

### `tests/integration/test_declared_config_reaches_the_workflow.py`

Drives the compiled graph twice with different declarations and asserts the **released response**
differs — the only assertion that distinguishes a live configuration path from a dead one.

| Test | Covers |
|---|---|
| Escalation threshold | Same history, different declaration, different released tier |
| Incident entry cap | Different summary; a cap of zero removes the history section |
| Default language | Changes the released response when the request names none |
| Request language wins | Over the declared default |
| Unusable declarations (5 parametrised) | Degrade to the documented behaviour rather than silently changing it |
| Bare graph | Still runs on the documented defaults |
| Store identifier reaches the lookup | Scoped versus unscoped |
| The bridge does not leak between requests | A scoped run does not seed the next |
| Japanese fault report | Reaches the equipment path, with the equipment class recognised in Japanese |

### `tests/integration/test_manifest_identity_alignment.py`

Both identities are **read** — the manifest from `config/agent.yaml`, the entry point's from the
provider the module actually bound. A test that spelled the expected namespace out twice would
keep passing through exactly the drift it exists to catch. A fourth case pins the direction
(`namespace == lower(industry)`), so re-aligning the wrong way fails rather than passing as a fix.

### `tests/integration/test_deploy_payload.py`

`deploy/invoke_payload.json` is posted verbatim by the deployment sign-off step, which asserts the
HTTP response was well-formed — not that the agent answered. Both hold for a request the agent
refuses. These tests assert the payload comes from this suite's own fixture and that driving it
through the entry contract produces a real domain response, plus a control proving the check can
fail.

## Proof-of-boundary tests

| PB-ID | Test | Expected result | Location |
|-------|------|----------------|----------|
| PB-4 | No platform SDK imports | 0 violations | `test_import_isolation.py` |
| PB-5 | State carries primitives only | No Pydantic, no credential-shaped field | `test_state_safety.py` |
| PB-6 | Full `invoke()` at the declared trust level follows the backbone order | `[InitializeNode, PreProcessNode, StoreOpsGraphNode, PostProcessNode, FinalizeNode]` | `test_pb_invoke_order.py` |
| PB-6 | The main slot is a `GraphNode` subclass | `StoreOpsGraphNode` | `test_pb_invoke_order.py` |
| PB-7 | Human-review interrupt propagation | Skipped — this template does not enable it | `test_pb7_hitl_interrupt_propagation.py` |

## Execution summary

- Runner: `python -m pytest tests/ -v`, against the framework wheel CI installs.
- 214 passed, 2 skipped. The two skips are the human-review propagation stubs, which are
  conditional on `hitl.enabled: true` in `config/config.yaml` and this template does not enable it.
