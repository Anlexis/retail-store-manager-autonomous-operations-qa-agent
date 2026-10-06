"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — IncidentMemoryRecallNode (inner DomainWorkflowGraph, step 2)
#
# Retrieves historical incidents for the store this request is scoped to. The
# store identifier is the namespace partition key, and it is the ONLY one this
# node will read — a request with no store scope reads nothing rather than
# falling back to a sentinel namespace.
#
# A deployment replaces the reference lookup below with a vector store or an
# incident-management API keyed on the same store identifier. The reference
# implementation is deterministic and makes no external call, so the downstream
# steps are exercised end to end without one.
#
# Incident entries carry equipment identifiers, incident descriptions and
# escalation records. Raw entries are never logged; the audit event carries
# counts only.
#
# Trust level: ANONYMOUS — inner domain step, downstream of the verified entry.

import logging
from datetime import date, timedelta, timezone
from datetime import datetime as _datetime
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.runtime_config import FALLBACK_STORE_OPS

logger = logging.getLogger(__name__)

# Store identifier that means "no store scope was established" rather than
# naming a store. No history is returned for it: reading a namespace under a
# sentinel would present another store's incidents as this one's.
_UNSCOPED_STORE = "UNKNOWN"


# ---------------------------------------------------------------------------
# Reference incident data
# ---------------------------------------------------------------------------


def _today() -> date:
    """Today's date, read at call time.

    Deliberately not a module constant: a module constant is evaluated once at
    import, so a long-running process would keep reporting the dates that were
    current when it started.
    """
    return _datetime.now(timezone.utc).date()


def _reference_incident_history(store_id: str, query_intent: str) -> List[Dict[str, Any]]:
    """Return the reference incident history for the given store and intent.

    A deployment replaces this with a vector-store or incident-management query
    partitioned by store_id. The reference implementation returns 0-2 entries
    depending on intent, so the downstream steps are exercised end to end
    without a live external call.
    """
    _TODAY = _today()
    if query_intent == "EQUIPMENT_FAULT":
        return [
            {
                "incident_id": f"{store_id}-EQ-2024-001",
                "date": str(_TODAY - timedelta(days=30)),
                "type": "EQUIPMENT_FAULT",
                "description": "[REDACTED for audit — see ITSM record]",
                "resolution": "Vendor maintenance dispatched; resolved in 4h",
                "escalated": True,
            },
            {
                "incident_id": f"{store_id}-EQ-2024-002",
                "date": str(_TODAY - timedelta(days=7)),
                "type": "EQUIPMENT_FAULT",
                "description": "[REDACTED for audit — see ITSM record]",
                "resolution": "Reset circuit breaker; self-resolved",
                "escalated": False,
            },
        ]
    if query_intent == "INCIDENT_LOG":
        return [
            {
                "incident_id": f"{store_id}-INC-2024-001",
                "date": str(_TODAY - timedelta(days=14)),
                "type": "INCIDENT_LOG",
                "description": "[REDACTED for audit — see ITSM record]",
                "resolution": "Reported to HQ; compliance check completed",
                "escalated": True,
            }
        ]
    # For other intents: no prior incident history retrieved.
    return []


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class IncidentMemoryRecallNode(FunctionNode):
    """Retrieve incident history from the store's memory namespace.

    Second node in the inner DomainWorkflowGraph for RET-C2-253.

    Only the store_id established for THIS request is used as the namespace
    key, so a caller cannot read another store's history. When no store scope
    was established the lookup returns nothing rather than reading a sentinel
    namespace — presenting some other store's incidents as this one's is the
    failure this node exists to prevent.

    Input state keys (seeded by the outer backbone, written by QueryParseNode):
        store_id:         str  — store namespace partition key
        query_intent:     str  — intent used to filter relevant incident types
        store_ops_config: dict — declared `max_incident_entries` cap

    Output state keys (partial dict — only changed keys):
        incident_history: list[dict]  — retrieved incidents (may be empty list)
        status:           AgentStatus
        error_log:        list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Read store_id, query_intent and the declared cap.
        # ------------------------------------------------------------------
        store_id: str = state.get("store_id") or _UNSCOPED_STORE
        query_intent: str = state.get("query_intent") or "GENERAL_OPS"
        store_ops = state.get("store_ops_config")
        if not isinstance(store_ops, dict):
            store_ops = dict(FALLBACK_STORE_OPS)
        max_entries = store_ops.get("max_incident_entries", FALLBACK_STORE_OPS["max_incident_entries"])
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 0:
            max_entries = int(FALLBACK_STORE_OPS["max_incident_entries"])

        # ------------------------------------------------------------------
        # 2. Retrieve the store's incident history.
        # ------------------------------------------------------------------
        incident_history: List[Dict[str, Any]] = []
        if store_id == _UNSCOPED_STORE:
            logger.info("IncidentMemoryRecallNode: no store scope on this request — history not read")
        else:
            try:
                incident_history = _reference_incident_history(store_id, query_intent)
            except Exception as exc:
                logger.error("IncidentMemoryRecallNode: retrieval failed: %s", exc)
                # Non-fatal: the downstream steps operate without history.
                incident_history = []
                error_log = error_log + ["IncidentMemoryRecallNode: incident history could not be read"]
                del exc

        # Structural cap. Without it the number of entries the downstream steps
        # summarise — and the length of the response — is set by whatever the
        # backing store returns.
        incident_history = incident_history[:max_entries]

        # ------------------------------------------------------------------
        # 3. Audit trace — counts only; raw incident descriptions are not logged.
        # ------------------------------------------------------------------
        logger.info(
            "IncidentMemoryRecallNode: store_id=%s intent=%s incidents_found=%d",
            store_id,
            query_intent,
            len(incident_history),
        )
        emit_trace_event(
            "incident_memory_recalled",
            {
                "store_id": store_id,
                "query_intent": query_intent,
                "incidents_found": len(incident_history),
                "max_incident_entries": max_entries,
                # Raw descriptions are NOT logged — counts only.
                "escalated_count": sum(1 for inc in incident_history if inc.get("escalated")),
            },
            state,
        )

        return {
            "incident_history": incident_history,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
