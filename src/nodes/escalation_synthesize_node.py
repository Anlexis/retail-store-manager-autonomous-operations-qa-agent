"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — EscalationSynthesizeNode (inner DomainWorkflowGraph, step 4)
#
# Synthesizes the primary domain response from SOP results and incident history.
# Sets escalation flags and generates the mandatory HQ incident record in Japanese.
#
# Escalation rules:
#   - EQUIPMENT_FAULT severity HIGH:   tier1_maintenance + tier2_hq
#   - EQUIPMENT_FAULT severity LOW:    tier1_maintenance only
#   - INCIDENT_LOG:                    tier2_hq (all incidents go to HQ)
#   - COMPLIANCE_QUERY:                no escalation
#   - Other:                           no escalation
#
# S-3 invariant enforced at synthesis time:
#   - If escalation_flag=True, escalation_tier must always be set.
#   - HQ incident record is ALWAYS in Japanese (JA) regardless of staff_lang.
#
# CONFIDENTIAL: hq_incident_record carries operational data.
# Audit trace emits only flags/tiers — never raw record content.
#
# S-4: audit trace emitted on every execution.
# Trust level: ANONYMOUS — inner domain node.

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.output_contract import ESCALATION_BANNER, ESCALATION_TIER_LABEL
from src.services.runtime_config import FALLBACK_STORE_OPS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Escalation determination
# ---------------------------------------------------------------------------

# Intents that always trigger escalation.
_ESCALATING_INTENTS = frozenset({"EQUIPMENT_FAULT", "INCIDENT_LOG"})


def _determine_escalation(
    query_intent: str,
    incident_history: List[Dict[str, Any]],
    history_threshold: int,
) -> Tuple[bool, Optional[str]]:
    """Return (escalation_flag: bool, escalation_tier: Optional[str]).

    For EQUIPMENT_FAULT: a fault that has already been escalated at this store
    at least *history_threshold* times is recurrent, and goes to head office
    rather than to maintenance alone.
    For INCIDENT_LOG: always escalates to head office.

    *history_threshold* is the declared `store_ops.escalation_history_threshold`,
    already bounds-checked by the runtime-config loader. It is an int by the time
    it arrives here, which matters: a NaN would make every comparison below False
    and quietly demote every recurrent fault to the lower tier, with nothing
    reporting an error.
    """
    if query_intent not in _ESCALATING_INTENTS:
        return False, None

    if query_intent == "EQUIPMENT_FAULT":
        prior_escalated = sum(1 for inc in incident_history if inc.get("escalated"))
        if prior_escalated >= history_threshold:
            return True, "tier2_hq"
        return True, "tier1_maintenance"

    if query_intent == "INCIDENT_LOG":
        return True, "tier2_hq"

    return False, None


# ---------------------------------------------------------------------------
# Response synthesis
# ---------------------------------------------------------------------------


def _synthesize_response(
    query_intent: str,
    sop_results: Dict[str, Any],
    incident_history: List[Dict[str, Any]],
    escalation_flag: bool,
    escalation_tier: Optional[str],
    incident_type_hint: Optional[str],
) -> str:
    """Build the primary response text (pre-multilingual adaptation).

    Incorporates SOP procedures, relevant incident history summary,
    and escalation instructions if needed.
    """
    lines: List[str] = []

    procedures = sop_results.get("procedures", [])
    if procedures:
        lines.append("Recommended procedure:")
        for step in procedures:
            lines.append(f"  {step}")

    if incident_history:
        lines.append(f"\nStore history: {len(incident_history)} similar incident(s) on record.")
        # Surface resolution hints from most recent incident only (not raw descriptions).
        latest = incident_history[-1]
        resolution = latest.get("resolution", "")
        if resolution:
            lines.append(f"Previous resolution: {resolution}")

    if escalation_flag and escalation_tier:
        contacts = sop_results.get("contacts", [])
        # The banner and the tier label are the strings the response boundary
        # verifies are still present in whatever is released. They live in the
        # output contract module so the writer and the verifier cannot drift.
        lines.append(f"\n{ESCALATION_BANNER}")
        lines.append(f"{ESCALATION_TIER_LABEL} {escalation_tier}")
        for contact in contacts:
            if isinstance(contact, dict) and contact.get("name"):
                phone = contact.get("phone", "")
                lines.append(f"Contact: {contact['name']}" + (f" ({phone})" if phone else ""))

    compliance_items = sop_results.get("compliance_items", [])
    if compliance_items:
        lines.append("\nCompliance checklist:")
        for item in compliance_items:
            lines.append(f"  {item}")

    return "\n".join(lines) if lines else "Refer to the Store Operations Manual for guidance."


def _generate_hq_record_ja(
    query_intent: str,
    escalation_tier: Optional[str],
    incident_type_hint: Optional[str],
    sop_id: str,
) -> Optional[str]:
    """Generate the HQ incident record in Japanese.

    This record is ALWAYS in Japanese, regardless of staff_lang (safety requirement).
    Returns None for intents that do not require HQ reporting.
    """
    if query_intent not in _ESCALATING_INTENTS:
        return None

    equipment_str = f"機器種別: {incident_type_hint}" if incident_type_hint else "機器種別: 不明"
    tier_str = escalation_tier or "tier1_maintenance"

    return (
        f"【本部報告書】\n"
        f"種別: {query_intent}\n"
        f"{equipment_str}\n"
        f"エスカレーション区分: {tier_str}\n"
        f"対応SOP: {sop_id}\n"
        f"状態: 対応中\n"
        f"※本記録は自動生成されました。店舗担当者による確認が必要です。"
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class EscalationSynthesizeNode(FunctionNode):
    """Synthesize the response and set escalation flags.

    Fourth node in the inner DomainWorkflowGraph for RET-C2-253.

    Input state keys (written by earlier nodes):
        query_intent:       str       — classified intent
        incident_type_hint: str|None  — equipment hint
        sop_results:        dict      — KB search results
        incident_history:   list      — store incident history

    Output state keys (partial dict — only changed keys):
        escalation_flag:    bool
        escalation_tier:    str|None
        hq_incident_record: str|None  — always in Japanese
        adapted_response:   str       — draft response (pre-multilingual)
        status:             AgentStatus
        error_log:          list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Read inputs.
        # ------------------------------------------------------------------
        query_intent: str = state.get("query_intent") or "GENERAL_OPS"
        incident_type_hint: Optional[str] = state.get("incident_type_hint")
        sop_results: Dict[str, Any] = state.get("sop_results") or {
            "procedures": [],
            "contacts": [],
            "compliance_items": [],
            "sop_id": "SOP-GEN-001",
        }
        incident_history: List[Dict[str, Any]] = list(state.get("incident_history") or [])

        store_ops = state.get("store_ops_config")
        if not isinstance(store_ops, dict):
            store_ops = dict(FALLBACK_STORE_OPS)
        threshold = store_ops.get(
            "escalation_history_threshold",
            FALLBACK_STORE_OPS["escalation_history_threshold"],
        )
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            threshold = int(FALLBACK_STORE_OPS["escalation_history_threshold"])

        # ------------------------------------------------------------------
        # 2. Determine escalation.
        # ------------------------------------------------------------------
        escalation_flag, escalation_tier = _determine_escalation(query_intent, incident_history, threshold)

        # ------------------------------------------------------------------
        # 3. Synthesize primary response.
        # ------------------------------------------------------------------
        adapted_response = _synthesize_response(
            query_intent,
            sop_results,
            incident_history,
            escalation_flag,
            escalation_tier,
            incident_type_hint,
        )

        # ------------------------------------------------------------------
        # 4. Generate HQ incident record in Japanese (S-3: always JA).
        # ------------------------------------------------------------------
        hq_incident_record = _generate_hq_record_ja(
            query_intent,
            escalation_tier,
            incident_type_hint,
            sop_results.get("sop_id", "SOP-GEN-001"),
        )

        # ------------------------------------------------------------------
        # 5. S-4 audit trace (no raw incident/HQ record content — flags only).
        # ------------------------------------------------------------------
        logger.info(
            "EscalationSynthesizeNode: intent=%s escalation_flag=%s tier=%s hq_record=%s",
            query_intent,
            escalation_flag,
            escalation_tier,
            hq_incident_record is not None,
        )
        emit_trace_event(
            "escalation_synthesized",
            {
                "query_intent": query_intent,
                "escalation_flag": escalation_flag,
                "escalation_tier": escalation_tier,
                "hq_record_generated": hq_incident_record is not None,
                "incident_history_count": len(incident_history),
                "escalation_history_threshold": threshold,
                # Raw adapted_response and hq_incident_record NOT logged (CONFIDENTIAL).
            },
            state,
        )

        return {
            "escalation_flag": escalation_flag,
            "escalation_tier": escalation_tier,
            "hq_incident_record": hq_incident_record,
            "adapted_response": adapted_response,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
