"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — ResponseFormatNode (inner DomainWorkflowGraph, step 6 / last)
#
# Assembles the final structured output dict from all domain state fields.
# This is the last node in the inner domain pipeline; its output is returned
# by DomainWorkflowGraph.get_output() as sub_result and consumed by
# StoreOpsGraphNode.merge_output() in graph.py.
#
# Output contract (designed together with graph.py merge_output):
#   formatted_response: {
#     response_in_staff_lang: str,         — the final response text
#     hq_incident_record_ja:  str | None,  — JA HQ record (if applicable)
#     escalation_flag:        bool,
#     escalation_tier:        str | None,
#     query_intent:           str,
#     memory_updated:         bool,        — whether this request wrote back to
#                                            the store's incident memory. The
#                                            reference implementation reads only,
#                                            so it is always False.
#   }
#
# S-4: audit trace emitted on every execution.
# Trust level: ANONYMOUS — inner domain node.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class ResponseFormatNode(FunctionNode):
    """Assemble the final formatted response dict.

    Last node in the inner DomainWorkflowGraph for RET-C2-253.

    Input state keys (written by all prior domain nodes):
        adapted_response:   str       — multilingual-adapted response text
        hq_incident_record: str|None  — Japanese HQ incident record
        escalation_flag:    bool
        escalation_tier:    str|None
        query_intent:       str
        out_of_scope:       bool

    Output state keys (partial dict — only changed keys):
        formatted_response: dict — final structured output
        status:             AgentStatus
        error_log:          list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Read domain state fields.
        # ------------------------------------------------------------------
        adapted_response: str = state.get("adapted_response") or ""
        hq_incident_record: Optional[str] = state.get("hq_incident_record")
        escalation_flag: bool = bool(state.get("escalation_flag", False))
        escalation_tier: Optional[str] = state.get("escalation_tier")
        query_intent: str = state.get("query_intent") or "GENERAL_OPS"
        out_of_scope: bool = bool(state.get("out_of_scope", False))

        # ------------------------------------------------------------------
        # 2. Ensure a response is always present.
        #    Every earlier step supplies one, so this is the last-resort floor
        #    rather than an ordinary path — the response boundary refuses an
        #    empty response outright, so releasing nothing is not an option.
        # ------------------------------------------------------------------
        if not adapted_response.strip():
            adapted_response = (
                "Refer to the Store Operations Manual (SOM) for guidance. "
                "Contact HQ Operations Support if further assistance is needed."
            )

        # ------------------------------------------------------------------
        # 3. Assemble formatted_response dict.
        # ------------------------------------------------------------------
        formatted_response: Dict[str, Any] = {
            "response_in_staff_lang": adapted_response,
            "hq_incident_record_ja": hq_incident_record,
            "escalation_flag": escalation_flag,
            "escalation_tier": escalation_tier,
            "query_intent": query_intent,
            # The reference incident lookup reads only; a deployment that writes
            # back to the store's incident memory sets this True.
            "memory_updated": False,
        }

        # ------------------------------------------------------------------
        # 4. Audit trace — metadata only; no raw response content.
        # ------------------------------------------------------------------
        logger.info(
            "ResponseFormatNode: intent=%s escalation=%s out_of_scope=%s " "hq_record=%s response_len=%d",
            query_intent,
            escalation_flag,
            out_of_scope,
            hq_incident_record is not None,
            len(adapted_response),
        )
        emit_trace_event(
            "response_formatted",
            {
                "query_intent": query_intent,
                "escalation_flag": escalation_flag,
                "escalation_tier": escalation_tier,
                "hq_record_present": hq_incident_record is not None,
                "out_of_scope": out_of_scope,
                "response_length": len(adapted_response),
            },
            state,
        )

        return {
            "formatted_response": formatted_response,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
