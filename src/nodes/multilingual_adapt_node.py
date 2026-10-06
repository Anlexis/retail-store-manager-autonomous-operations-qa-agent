"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — MultilingualAdaptNode (inner DomainWorkflowGraph, step 5)
#
# Adapts the synthesized response to the staff member's language preference.
# For SAFETY-CRITICAL intents (EQUIPMENT_FAULT, INCIDENT_LOG), the Japanese
# original procedure is ALWAYS appended after the translated text, regardless
# of staff_lang.  This ensures HQ-level accuracy even if LLM translation
# quality degrades.
#
# Language support: ja (primary), en, vi, tl (育成就労 languages)
# The reference implementation returns deterministic adaptations per language
# code; a deployment substitutes a translation backend behind the same call.
#
# S-4: audit trace emitted on every execution.
# Trust level: ANONYMOUS — inner domain node.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language adaptation helpers
# ---------------------------------------------------------------------------

# Intents that require JA appendix for safety-critical instructions.
_SAFETY_CRITICAL_INTENTS = frozenset({"EQUIPMENT_FAULT", "INCIDENT_LOG"})

# Language header strings for each supported language.
_LANG_HEADERS: Dict[str, str] = {
    "ja": "",  # Japanese is the primary — no header needed
    "en": "[English] ",
    "vi": "[Tiếng Việt] ",
    "tl": "[Filipino] ",
}

# The safety escalation instruction in each supported language.
_ESCALATION_TRANSLATIONS: Dict[str, str] = {
    "ja": "警告: この問題はエスカレーションが必要です。担当者に連絡してください。",
    "en": "WARNING: This issue requires escalation. Contact the responsible party immediately.",
    "vi": "CẢNH BÁO: Vấn đề này cần leo thang. Hãy liên hệ ngay với người phụ trách.",
    "tl": "BABALA: Ang isyung ito ay nangangailangan ng escalation. Makipag-ugnayan sa responsableng tao.",
}

# JA safety appendix label.
_JA_APPENDIX_HEADER = "\n\n--- 安全のために日本語手順を以下に添付します ---\n"


def _adapt_to_language(
    response_text: str,
    staff_lang: str,
    query_intent: str,
    escalation_flag: bool,
) -> str:
    """Return response text adapted to staff_lang.

    For non-JA staff_lang with safety-critical intents, appends the JA
    original procedure after the translated text.

    Returns a deterministic adaptation. A deployment substitutes a translation
    backend behind this call; the language contract and the Japanese appendix
    rule below are the parts that do not change.
    """
    if staff_lang == "ja" or staff_lang not in _LANG_HEADERS:
        # Japanese or unsupported code — return as-is (primary language).
        return response_text

    header = _LANG_HEADERS.get(staff_lang, "")

    # For a non-Japanese reader, lead with the translated escalation warning
    # where one applies and keep the procedure steps below it.
    adapted_lines: List[str] = [f"{header}{response_text}"]

    if escalation_flag:
        escalation_warning = _ESCALATION_TRANSLATIONS.get(staff_lang, _ESCALATION_TRANSLATIONS["en"])
        adapted_lines.insert(0, escalation_warning)

    adapted = "\n".join(adapted_lines)

    # Append Japanese original for safety-critical intents (architect requirement).
    if query_intent in _SAFETY_CRITICAL_INTENTS:
        adapted += _JA_APPENDIX_HEADER + response_text

    return adapted


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class MultilingualAdaptNode(FunctionNode):
    """Adapt the synthesized response to the staff member's language.

    Fifth node in the inner DomainWorkflowGraph for RET-C2-253.

    For EQUIPMENT_FAULT and INCIDENT_LOG intents, the Japanese procedure is
    always appended after the translated text for safety assurance.

    `staff_lang` reaches this step through the graph's initial state, seeded
    from the request contract the outer backbone validated. It arrived as None
    on every request before that path existed, so this step always ran in the
    primary language and the adaptation never fired.

    Input state keys (seeded by the outer backbone, written by earlier steps):
        adapted_response: str      — draft response from EscalationSynthesizeNode
        staff_lang:       str      — language code (ja/en/vi/tl)
        query_intent:     str      — classified intent
        escalation_flag:  bool     — whether escalation is required

    Output state keys (partial dict — only changed keys):
        adapted_response: str  — updated multilingual response
        status:           AgentStatus
        error_log:        list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Read inputs.
        # ------------------------------------------------------------------
        adapted_response: str = state.get("adapted_response") or ""
        staff_lang: str = state.get("staff_lang") or "ja"
        query_intent: str = state.get("query_intent") or "GENERAL_OPS"
        escalation_flag: bool = bool(state.get("escalation_flag", False))

        if not adapted_response.strip():
            # Nothing to adapt — return as-is with a warning.
            logger.warning("MultilingualAdaptNode: adapted_response is empty; skipping adaptation")
            return {
                "adapted_response": "",
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        # ------------------------------------------------------------------
        # 2. Language adaptation.
        # ------------------------------------------------------------------
        updated_response = _adapt_to_language(adapted_response, staff_lang, query_intent, escalation_flag)

        # ------------------------------------------------------------------
        # 3. S-4 audit trace (response content not logged — length only).
        # ------------------------------------------------------------------
        ja_appended = staff_lang != "ja" and query_intent in _SAFETY_CRITICAL_INTENTS
        logger.info(
            "MultilingualAdaptNode: staff_lang=%s intent=%s ja_appended=%s",
            staff_lang,
            query_intent,
            ja_appended,
        )
        emit_trace_event(
            "multilingual_adapted",
            {
                "staff_lang": staff_lang,
                "query_intent": query_intent,
                "escalation_flag": escalation_flag,
                "ja_appendix_added": ja_appended,
                "response_length": len(updated_response),
            },
            state,
        )

        return {
            "adapted_response": updated_response,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
