# Template Design Specification

## Position in AgentCore Architecture

| Layer | Choice |
|---|---|
| Agent class | `StoreManagerOpsQAAgent` |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Pattern | Cat 2 two-layer nested: outer backbone + inner domain workflow |

Three-layer separation:

- **State** — a flat `TypedDict` (never a Pydantic model: checkpoints are msgpack-serialised and
  arbitrary objects corrupt silently).
- **Node** — framework inheritance; a node overrides `execute(self, state) -> dict` and returns
  only the keys it changed. The framework's node wrapper never passes a `config` argument, so a
  node reads declared values from state, not from an argument.
- **Graph** — composition: `register_nodes()` substitutes nodes into the fixed backbone.

## Architecture Overview

RET-C2-253 answers store managers' operational questions: equipment faults, incident logging,
compliance deadlines, and procedures for staff. It classifies the question, reads that store's
incident history, retrieves the matching standard operating procedure, decides whether the
situation escalates and to whom, writes the answer in the staff member's language, and files a
head-office record in Japanese where one is required.

### Two-Layer Architecture

```
Outer backbone (AgentBaseGraph — fixed 5-node pipeline):
  START → initialize → pre_process → main (StoreOpsGraphNode) → post_process → finalize → END

Inner domain workflow (BaseGraph — 6 domain steps):
  START → query_parse → incident_memory_recall → sop_kb_search
        → escalation_synthesize → multilingual_adapt → response_format → END
```

### Crossing the nested-graph boundary

The framework hands a nested graph exactly one thing: a string. `subgraph.invoke()` receives
`user_input` and nothing else — no state, no request context. So everything the outer backbone
validated has to be carried across deliberately.

`src/graph/context_bridge.py` does that. `StoreOpsGraphNode.extract_input()` returns the
**question text** and publishes the validated contract (store identifier, staff language, the
resolved runtime block) on a `ContextVar`; `DomainWorkflowGraph._extra_initial_state()` consumes
it — clearing it on read — before the first step runs.

Two design points follow from the shape of that boundary:

- **The string that crosses must be the question.** Passing a serialised envelope instead makes
  the classifier read the envelope. JSON serialisation escapes non-ASCII, so a Japanese question
  arrives with no Japanese left in it and the only matchable keywords are the literal key names.
- **Reading clears.** One request's contract cannot be read twice or inherited by the next, and a
  direct invocation of the inner graph starts from the documented defaults rather than from a
  previous caller's store.

### Node Configuration

#### Outer backbone (AgentBaseGraph)

| Node | Class | Trust Level | Responsibility | Input State | Output State |
|------|-------|------------|----------------|-------------|--------------|
| initialize | InitializeNode (framework) | — | Sets schema_version, session_id, trust_level | user_input | correlation_id, session_id |
| pre_process | PreProcessNode | VERIFIED_EXTERNAL | Input boundary: bound and screen the question, validate the request context, resolve store_id and staff_lang | user_input, input_context, store_ops_config | validated_input, store_id, staff_lang |
| main | StoreOpsGraphNode (GraphNode) | — | Publishes the contract on the bridge, delegates to DomainWorkflowGraph, maps sub_result back | validated_input, store_ops_config | result (formatted_response), status |
| post_process | PostProcessNode | ANONYMOUS | Response boundary: verify the output contract, release or withhold | result | formatted_output, status, cleared fields |
| finalize | FinalizeNode (framework) | — | Builds response_metadata, total_time_ms | status | response_metadata |

#### Inner domain workflow (BaseGraph)

| Node | Class | Trust Level | Responsibility | Input State | Output State |
|------|-------|------------|----------------|-------------|--------------|
| query_parse | QueryParseNode | ANONYMOUS | Classify the question; extract the equipment class | validated_input, user_input | query_intent, incident_type_hint |
| incident_memory_recall | IncidentMemoryRecallNode | ANONYMOUS | Read this store's incident history, capped | store_id, query_intent, store_ops_config | incident_history |
| sop_kb_search | SOPKBSearchNode | ANONYMOUS | Retrieve the procedure, contacts and compliance items for the question class | query_intent, incident_type_hint | sop_results |
| escalation_synthesize | EscalationSynthesizeNode | ANONYMOUS | Decide the escalation tier, write the answer, generate the Japanese head-office record | sop_results, incident_history, query_intent, store_ops_config | escalation_flag, escalation_tier, hq_incident_record, adapted_response |
| multilingual_adapt | MultilingualAdaptNode | ANONYMOUS | Adapt the answer to staff_lang; append the Japanese original for safety-critical classes | adapted_response, staff_lang, escalation_flag | adapted_response (updated) |
| response_format | ResponseFormatNode | ANONYMOUS | Assemble the released response | adapted_response, escalation_flag, hq_incident_record | formatted_response |

### Data Flow

```
user_input + input_context
    ↓
[PreProcessNode] — VERIFIED_EXTERNAL
    validated_input = {query_text, store_id, staff_lang}
    ↓  (question text + contract via the context bridge)
[StoreOpsGraphNode] → DomainWorkflowGraph
    ├── [QueryParseNode]             → query_intent, incident_type_hint
    ├── [IncidentMemoryRecallNode]   → incident_history (this store only, capped)
    ├── [SOPKBSearchNode]            → sop_results
    ├── [EscalationSynthesizeNode]   → escalation_flag, escalation_tier,
    │                                  hq_incident_record (Japanese), adapted_response
    ├── [MultilingualAdaptNode]      → adapted_response (adapted + Japanese appendix)
    └── [ResponseFormatNode]         → formatted_response
    ↓
merge_output: result = formatted_response
    ↓
[PostProcessNode] — response boundary
    ↓
formatted_output (released) — or the withheld notice, with every
output-bearing field cleared
```

### State Definition

| Field | Type | Purpose | Written by |
|-------|------|---------|-----------|
| validated_input | Optional[Dict[str, Any]] | Structured question payload {query_text, store_id, staff_lang} | PreProcessNode |
| store_id | Optional[str] | Incident-history namespace key | PreProcessNode |
| staff_lang | Optional[str] | Staff language code (ja/en/vi/tl) | PreProcessNode |
| store_ops_config | Optional[Dict[str, Any]] | The bounds-checked `store_ops` block from config/config.yaml | the graph's initial-state hook |
| query_intent | Optional[str] | Classified question class | QueryParseNode |
| incident_type_hint | Optional[str] | Canonical equipment class | QueryParseNode |
| incident_history | Optional[List[Dict[str, Any]]] | This store's incident history (confidential) | IncidentMemoryRecallNode |
| sop_results | Optional[Dict[str, Any]] | Procedures, contacts, compliance items | SOPKBSearchNode |
| escalation_flag | bool | Escalation required | EscalationSynthesizeNode |
| escalation_tier | Optional[str] | tier1_maintenance / tier2_hq / emergency | EscalationSynthesizeNode |
| hq_incident_record | Optional[str] | Head-office record, always Japanese (confidential) | EscalationSynthesizeNode |
| adapted_response | Optional[str] | The answer text, before and after language adaptation | EscalationSynthesizeNode, MultilingualAdaptNode |
| formatted_response | Optional[Dict[str, Any]] | The assembled response | ResponseFormatNode |
| out_of_scope | bool | Audit flag, reserved | PreProcessNode, QueryParseNode |
| status | Optional[str] | AgentStatus value | every step |
| error_log | List[str] | Accumulated refusal reasons, closed-set labels | every step |
| trace_id | Optional[str] | Audit correlation id | InitializeNode (framework) |

State constraints:

- Flat `TypedDict` only — primitives and JSON-serialisable values.
- No credentials, tokens or keys in state: checkpoints persist it.
- No Pydantic models, dataclasses or arbitrary objects (msgpack-incompatible).

## Runtime configuration

`config/agent.yaml` is the static manifest and holds no runtime values.
`config/config.yaml` holds everything tunable, and `src/services/runtime_config.py` is the single
reader.

Nodes receive state, never the graph, and the framework's node wrapper never supplies the optional
`config` argument. So a declared value reaches a step through exactly one route:

```
Graph(config=agent_config())
   → StoreManagerOpsQAAgent._extra_initial_state()   (seeds store_ops_config)
   → outer state
   → the context bridge
   → DomainWorkflowGraph._extra_initial_state()
   → inner state
   → the step that reads it
```

Any break in that chain leaves the declaration dead while everything stays green, which is why
`tests/integration/test_declared_config_reaches_the_workflow.py` asserts on the **released
response** rather than on a loaded value.

Every number is read through a finite-and-bounded parser. `float()` accepts `"NaN"` and
`"Infinity"`, and every comparison against NaN is False — a NaN escalation threshold would make
`prior_escalated >= threshold` False for every store, quietly demoting every recurrent fault to
the lower tier with nothing reporting an error. Out-of-range and non-finite values degrade to the
documented default: fail closed.

## Security boundaries

### Input boundary — PreProcessNode (VERIFIED_EXTERNAL)

The screens live in `src/services/caller_contract.py` and are applied **in the node**, not only in
the HTTP adapter: behind the platform gateway the adapter is not in the path at all, so a
guarantee that lives only there is absent in production. The adapter applies the same functions,
so the two cannot drift.

- The question is bounded (5–4,000 characters) before anything parses it.
- Disallowed instruction patterns are screened as classes, not as a keyword list: prompt-injection
  phrases, chat-template control tokens (`<|…|>`, `[INST]`, `<<SYS>>`), and statement-shaped SQL.
  Every string is screened raw AND markup-stripped — a markup strip can turn a detectable control
  token into readable directive text — and invisible formatting characters are resolved both by
  dropping them (a directive broken mid-word) and by turning them into spaces (a directive whose
  spaces were replaced by them).
- Patterns are anchored on phrases and structure. This domain says "insert into the logbook" and
  "act as duty manager" in ordinary sentences; an unanchored screen would refuse real work, which
  is the failure mode that actually stops the store.
- Refusals name the field and never echo the value or the matched marker.

### Request context — a closed, inert channel

`input_context` accepts `store_id` and `staff_lang` only. An undeclared key is **refused**, not
ignored: ignoring is not stripping, and an ignored key stays on the context channel, is returned
verbatim inside the framework's first node's own result, and fails the whole run on the
framework's output scan with an error the caller cannot act on. A credential-shaped value fails
the same way, so it is refused readably at the boundary instead.

The channel is restricted to inert identifiers by design. Free text there would be exposed to that
hazard on every request; identifiers cannot carry it.

### Store scope — IncidentMemoryRecallNode

Only the store identifier established for this request is used as the namespace key. A request
with no store scope reads **nothing** rather than reading a sentinel namespace — presenting some
other store's incidents as this one's is the failure this step exists to prevent. The entry count
is capped by the declared `max_incident_entries`.

### Response boundary — PostProcessNode

The contract is in `src/services/output_contract.py`, which also holds the escalation marker
strings the synthesis step writes — one module, so the writer and the verifier cannot drift.

1. A response is present and non-empty.
2. When the workflow flags an escalation, the released response still **carries the escalation
   instruction** and a tier from the known set. A manager must never be told "power-cycle the
   unit" with the "call the contractor" half removed.
3. An equipment fault or a logged incident always files a head-office record, and that record is
   written in Japanese whatever language the staff member reads.
4. Nothing credential-shaped is released. The check is the **union** of the framework's own
   detector and this template's assignment shapes (`password=`, `api_key:`): a narrower local set
   lets the framework raise inside this node, and the framework's error handler then discards this
   node's entire update — so a detector gap is a containment bypass; delegating entirely to the
   framework would drop the assignment shapes it does not describe, making the screen narrower
   while looking like a tightening.

**Withholding means clearing, not raising.** The framework's envelope resolves
`formatted_output or result`, on an error status as well as a successful one — so a boundary that
returns an error while leaving the assembled response in `result` still ships it inside the error
envelope. On a violation this node returns an error status, clears every output-bearing field,
and puts a **truthy** notice in `formatted_output`; a falsy replacement would re-open the fallback
onto the text being withheld. The reason is one of a closed set of labels, never text built from
the offending content.

**No numeric precision grid applies here.** That invariant exists for templates that render
monetary aggregates. This one releases procedure text, an escalation tier from a closed set,
counts, and the Japanese record — there is no money in the output to round, so the invariants
above are what the boundary enforces instead.

### Audit

Every `execute()` emits a domain event through `emit_trace_event(name, payload, state)`:
`pre_process_validated`, `pre_process_rejected`, `pre_process_injection_blocked`,
`pre_process_credential_blocked`, `query_parsed`, `incident_memory_recalled`, `sop_kb_searched`,
`escalation_synthesized`, `multilingual_adapted`, `response_formatted`,
`post_process_output_released`, `post_process_output_withheld`.

Confidential values — incident entries, the head-office record, the answer text — never appear in
an audit payload. Counts, flags, lengths and closed-set labels do.

### Credentials

No credentials or secrets in state. The secret provider is scoped by the identity the manifest
declares: `secrets_factory(namespace="ret", agent_name="StoreManagerOpsQAAgent")`, where
`namespace` is `lower(industry)`. A namespace that disagrees with the manifest splits one agent's
secrets across two stores with no boot-time symptom, so
`tests/integration/test_manifest_identity_alignment.py` reads both sides from their files and
compares them.

## Multilingual safety requirement

For `EQUIPMENT_FAULT` and `INCIDENT_LOG`, MultilingualAdaptNode appends the Japanese procedure
after the adapted text whatever `staff_lang` says. A translation error must not be the only copy
of a safety instruction, and the head-office record is filed in Japanese regardless.

The equipment-class patterns match Japanese terms **without** word boundaries. `\b` is computed
over `\w`, which includes Kana and Kanji, so a Japanese term sitting between other Japanese
characters never has a boundary either side — and Japanese is written without spaces, so that is
the normal case rather than the corner one.

## Framework utilisation

- [x] `InvocationContext` — correlation_id, session_id, trust level, carried by the backbone
- [x] `SecurityViolationError` — available from `framework.errors`
- [x] `emit_trace_event()` — one domain event per step
- [x] `detect_credentials_in_value()` — the floor of the output boundary's credential check
- [x] `load_config()` — the manifest and runtime-config reader

### Composition pattern

- Outer `GraphNode` wrapping an inner `BaseGraph` subgraph.
- Composition target: `DomainWorkflowGraph`.
- Error strategy: `propagate` — an inner failure raises `SubgraphError` and the backbone routes to
  the error path rather than releasing a partial answer.

## Import isolation

- [x] No platform SDK imports.
- [x] Imports are `framework.*`, `shared.*` and `src.*` only.
- [x] No `.run()` — `.invoke()` only.

## Design decision record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | A fixed multi-step pipeline; no autonomous loop |
| Cat classification | Cat 1 | Cat 2 | Cat 2 | Retail-specific, multi-step, stateful |
| Inner topology | Linear | Conditional branching | Linear | The question class shapes node content, not graph edges |
| Multilingual approach | Translation only | Translation + Japanese appendix | Translation + appendix | A translation error must not be the only copy of a safety instruction |
| Memory scope | Per customer | Per store | Per store | Store-level operational patterns are the relevant context unit |
| Request context | Free text | Closed inert identifier set | Closed set | Free text on that channel is exposed to the framework's output scan on every request |
| Withholding a response | Raise | Clear the output-bearing fields | Clear | The envelope falls back to `result` even on an error status |
