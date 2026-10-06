"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# RET-C2-253 — Store Manager Autonomous Operations Q&A Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Data flow:
#   PreProcessNode (outer pre_process)
#     reads:  user_input
#     writes: validated_input, store_id, staff_lang
#
#   QueryParseNode (inner)
#     reads:  validated_input
#     writes: query_intent, incident_type_hint
#
#   IncidentMemoryRecallNode (inner)
#     reads:  store_id, query_intent
#     writes: incident_history
#
#   SOPKBSearchNode (inner)
#     reads:  validated_input, query_intent
#     writes: sop_results
#
#   EscalationSynthesizeNode (inner)
#     reads:  sop_results, incident_history, query_intent
#     writes: escalation_flag, escalation_tier, hq_incident_record, adapted_response
#
#   MultilingualAdaptNode (inner)
#     reads:  adapted_response, staff_lang, escalation_flag
#     writes: adapted_response (updated with translation / JA appendix)
#
#   ResponseFormatNode (inner)
#     reads:  adapted_response, escalation_flag, escalation_tier, hq_incident_record
#     writes: formatted_response
#
#   PostProcessNode (outer post_process)
#     reads:  result (mapped from formatted_response by merge_output)
#     writes: S-3 gated output
#
# Confidentiality note: incident_history and hq_incident_record carry
# store-operational data.  Downstream nodes must never log raw incident
# payloads; emit only masked trace tokens.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for RET-C2-253.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — PreProcessNode (pre_process slot)
    # ------------------------------------------------------------------

    # Validated and structured input dict produced by PreProcessNode (S-1).
    # Keys: store_id, staff_lang, query_text, incident_type_hint (optional).
    # Raw user_input is not persisted beyond PreProcessNode.
    validated_input: Optional[Dict[str, Any]]

    # Store identifier extracted and validated by PreProcessNode.
    # Used by IncidentMemoryRecallNode as the memory namespace key.
    store_id: Optional[str]

    # Staff member's language preference extracted by PreProcessNode.
    # Accepted values: "ja", "en", "vi", "tl".
    # Used by MultilingualAdaptNode for response language selection.
    staff_lang: Optional[str]

    # ------------------------------------------------------------------
    # Runtime configuration — seeded from config/config.yaml
    # ------------------------------------------------------------------

    # The bounds-checked `store_ops` block from config/config.yaml, seeded into
    # state by the graph so the domain steps can read a declared value. Nodes
    # receive state, never the graph, and the framework's node wrapper never
    # supplies the optional `config` argument on the node signature — so state
    # is the only route a declared value has into a step.
    # Keys: default_language, escalation_history_threshold, max_incident_entries.
    store_ops_config: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — QueryParseNode
    # ------------------------------------------------------------------

    # Classified query intent set by QueryParseNode.
    # Values: EQUIPMENT_FAULT | INCIDENT_LOG | COMPLIANCE_QUERY |
    #         STAFF_INSTRUCTION | GENERAL_OPS
    query_intent: Optional[str]

    # Optional equipment/incident type hint extracted from query text.
    # Passed to SOPKBSearchNode to narrow KB retrieval scope.
    incident_type_hint: Optional[str]

    # ------------------------------------------------------------------
    # Inner domain workflow — IncidentMemoryRecallNode
    # ------------------------------------------------------------------

    # Incident history retrieved from the store's memory namespace.
    # Each entry: {"incident_id": str, "date": str, "type": str,
    #              "description": str, "resolution": str, "escalated": bool}
    # CONFIDENTIAL: never log raw entries; emit only masked audit tokens.
    incident_history: Optional[List[Dict[str, Any]]]

    # ------------------------------------------------------------------
    # Inner domain workflow — SOPKBSearchNode
    # ------------------------------------------------------------------

    # SOP KB search results keyed by result type.
    # {"procedures": [...], "contacts": [...], "compliance_items": [...]}
    # Read by EscalationSynthesizeNode to build the domain response.
    sop_results: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner domain workflow — EscalationSynthesizeNode
    # ------------------------------------------------------------------

    # True when the query requires escalation to HQ or maintenance contractor.
    # Set by EscalationSynthesizeNode; read by MultilingualAdaptNode and
    # ResponseFormatNode.
    escalation_flag: bool

    # Escalation tier string when escalation_flag is True.
    # Values: "tier1_maintenance" | "tier2_hq" | "emergency".
    escalation_tier: Optional[str]

    # HQ incident record in Japanese, generated by EscalationSynthesizeNode
    # for EQUIPMENT_FAULT or INCIDENT_LOG intents.
    # Always in Japanese (JA) per safety-critical documentation requirement.
    # CONFIDENTIAL: never log raw content; emit masked tokens only.
    hq_incident_record: Optional[str]

    # Draft response text (pre-multilingual-adaptation).
    # Written by EscalationSynthesizeNode; updated by MultilingualAdaptNode.
    adapted_response: Optional[str]

    # ------------------------------------------------------------------
    # Inner domain workflow — ResponseFormatNode
    # ------------------------------------------------------------------

    # Final structured output dict assembled by ResponseFormatNode.
    # Keys: response_in_staff_lang (str), hq_incident_record_ja (str | None),
    #       escalation_flag (bool), escalation_tier (str | None),
    #       query_intent (str), memory_updated (bool).
    # Read by the outer merge_output() via DomainWorkflowGraph.get_output().
    formatted_response: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Control / routing flags
    # ------------------------------------------------------------------

    # True when the query is outside the store operations domain.
    # Out-of-scope is not a separate AgentStatus — status stays SUCCESS,
    # and ResponseFormatNode checks this flag to shape the caller response.
    out_of_scope: bool

    # ------------------------------------------------------------------
    # Status and audit — populated by all nodes
    # ------------------------------------------------------------------

    # AgentStatus string value ("SUCCESS" or "ERROR") set by each node.
    # Declared explicitly here so outer merge_output() can read it from
    # the inner graph's merged state without an attribute-access miss.
    status: Optional[str]

    # Accumulated error messages appended by any node that catches an
    # exception.  PostProcessNode gates on this list before writing output.
    error_log: List[str]

    # ------------------------------------------------------------------
    # Tracing / audit
    # ------------------------------------------------------------------

    # Correlation ID injected by InitializeNode for audit log correlation.
    # All nodes include this in every emit_trace_event() call.
    trace_id: Optional[str]
