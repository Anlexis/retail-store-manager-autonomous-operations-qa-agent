"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — SOPKBSearchNode (inner DomainWorkflowGraph, step 3)
#
# Retrieves relevant SOP (Standard Operating Procedure) knowledge from the
# store operations KB.  This is a RAG retrieval node:
#   - Input: validated_input.query_text + query_intent + incident_type_hint
#   - Output: sop_results dict with relevant procedures, contacts, and
#             compliance items for the classified intent
#
# A deployment points this at the operations knowledge base, scoped per store
# type. The reference implementation returns intent-keyed documents, so the
# downstream steps run end to end without a live retrieval backend.
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
# Reference SOP knowledge
# ---------------------------------------------------------------------------

# Maintenance contractor contact. Placeholder values — a deployment supplies the
# real contact from the operations knowledge base.
_MAINTENANCE_CONTACT = {
    "name": "CVS Equipment Services",
    "phone": "0120-XXX-XXX",
    "hours": "24h/7d",
    "sla_response_hours": 4,
}

# Head-office notification contact. Placeholder values, as above.
_HQ_CONTACT = {
    "name": "HQ Operations Support",
    "email": "ops-support@example.co.jp",
    "line_works_group": "store-ops-alerts",
}

# Food loss compliance template (食品ロス法 FY2026).
_FOOD_LOSS_COMPLIANCE_ITEMS = [
    "1. Record equipment failure time and date",
    "2. Document estimated food loss quantity (kg)",
    "3. Submit Form FL-2026 within 24 hours",
    "4. Attach photographic evidence",
    "5. Notify HQ via LINE Works alert",
]


def _reference_sop_results(query_intent: str, incident_type_hint: Optional[str]) -> Dict[str, Any]:
    """Return the reference SOP knowledge for the given intent.

    A deployment replaces this with a retrieval call against the store
    operations knowledge base (equipment manuals, escalation procedures,
    compliance checklists) keyed on the same intent and equipment hint.
    """
    if query_intent == "EQUIPMENT_FAULT":
        procedures = [
            "Step 1: Power-cycle the unit; wait 60 seconds.",
            "Step 2: Check the circuit breaker panel (B3-F panel, row 4).",
            "Step 3: If noise/leak persists after reset, do NOT operate; cordon off.",
            "Step 4: Call maintenance contractor immediately.",
            "Step 5: Log incident in store incident system.",
        ]
        if incident_type_hint and "cooler" in incident_type_hint:
            procedures.insert(
                1,
                "Step 1a: Move perishables to backup cooler (aisle 7) immediately.",
            )
        return {
            "procedures": procedures,
            "contacts": [_MAINTENANCE_CONTACT, _HQ_CONTACT],
            "compliance_items": _FOOD_LOSS_COMPLIANCE_ITEMS,
            "sop_id": "SOP-EQ-001",
        }

    if query_intent == "INCIDENT_LOG":
        return {
            "procedures": [
                "Step 1: Record incident date, time, location.",
                "Step 2: Describe what happened (who, what, where, when, how).",
                "Step 3: Document any injuries, property damage.",
                "Step 4: Notify HQ within 2 hours of incident.",
                "Step 5: Preserve evidence; do not alter the scene.",
            ],
            "contacts": [_HQ_CONTACT],
            "compliance_items": [],
            "sop_id": "SOP-INC-001",
        }

    if query_intent == "COMPLIANCE_QUERY":
        return {
            "procedures": [
                "Food Loss Compliance (食品ロス削減推進法 FY2026):",
                "Monthly report due: 5th of following month.",
                "Items: total food waste (kg), category breakdown, disposal method.",
                "Submit via: regional operations portal.",
            ],
            "contacts": [_HQ_CONTACT],
            "compliance_items": _FOOD_LOSS_COMPLIANCE_ITEMS,
            "sop_id": "SOP-COMP-001",
        }

    if query_intent == "STAFF_INSTRUCTION":
        return {
            "procedures": [
                "Refer staff to the Store Operations Manual (SOM) section 3.",
                "For 育成就労 staff: multilingual quick-reference cards are in the break room.",
                "Training schedule: Tuesday and Thursday 09:00–10:00.",
            ],
            "contacts": [],
            "compliance_items": [],
            "sop_id": "SOP-STAFF-001",
        }

    # GENERAL_OPS fallback
    return {
        "procedures": [
            "Please refer to the Store Operations Manual (SOM) for detailed procedures.",
            "Contact the store manager or HQ Operations Support for urgent matters.",
        ],
        "contacts": [_HQ_CONTACT],
        "compliance_items": [],
        "sop_id": "SOP-GEN-001",
    }


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class SOPKBSearchNode(FunctionNode):
    """Retrieve SOP knowledge for the classified query intent.

    Third node in the inner DomainWorkflowGraph for RET-C2-253.

    Input state keys (written by earlier nodes):
        validated_input:    dict — {query_text, store_id, staff_lang}
        query_intent:       str  — classified intent
        incident_type_hint: str  — optional equipment type hint

    Output state keys (partial dict — only changed keys):
        sop_results: dict — {procedures, contacts, compliance_items, sop_id}
        status:      AgentStatus
        error_log:   list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Read inputs.
        # ------------------------------------------------------------------
        query_intent: str = state.get("query_intent") or "GENERAL_OPS"
        incident_type_hint: Optional[str] = state.get("incident_type_hint")
        validated_input = state.get("validated_input") or {}
        store_id: str = str(validated_input.get("store_id", "UNKNOWN"))

        # ------------------------------------------------------------------
        # 2. Retrieve the SOP knowledge for this intent.
        # ------------------------------------------------------------------
        try:
            sop_results = _reference_sop_results(query_intent, incident_type_hint)
        except Exception as exc:
            logger.error("SOPKBSearchNode: knowledge retrieval failed: %s", exc)
            del exc
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + ["SOPKBSearchNode: SOP knowledge could not be retrieved"],
            }

        # ------------------------------------------------------------------
        # 3. Audit trace.
        # ------------------------------------------------------------------
        logger.info(
            "SOPKBSearchNode: intent=%s sop_id=%s store_id=%s procedures=%d",
            query_intent,
            sop_results.get("sop_id"),
            store_id,
            len(sop_results.get("procedures", [])),
        )
        emit_trace_event(
            "sop_kb_searched",
            {
                "query_intent": query_intent,
                "incident_type_hint": incident_type_hint,
                "store_id": store_id,
                "sop_id": sop_results.get("sop_id"),
                "procedures_count": len(sop_results.get("procedures", [])),
                "contacts_count": len(sop_results.get("contacts", [])),
            },
            state,
        )

        return {
            "sop_results": sop_results,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
