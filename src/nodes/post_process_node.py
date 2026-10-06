"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — PostProcessNode (outer backbone, post_process slot)
#
# The response boundary. It verifies the assembled response against the output
# contract (src/services/output_contract.py) and releases it, or withholds it.
#
# WITHHOLDING MEANS CLEARING, NOT RAISING. The framework's envelope resolves
# `formatted_output or result`, and it does so on an error status as well as a
# successful one. A boundary that returns an error while leaving the assembled
# response in `result` therefore still ships that response to the caller, inside
# the error envelope. So on a violation this node returns an error status, clears
# every output-bearing field, and puts a TRUTHY notice in `formatted_output` — a
# falsy replacement would re-open the fallback onto the very text being withheld.
#
# The credential half of the check is the union of the framework's own detector
# and this template's assignment shapes. That matters in both directions: a
# narrower local set lets the framework raise INSIDE this node, and the
# framework's error handler then discards this node's entire update — the
# clearing included — so a detector gap is a containment bypass; and delegating
# entirely to the framework would drop the `password=` shape it does not
# describe, making the screen narrower while looking like a tightening.
#
# Trust level: ANONYMOUS — downstream of the verified external entry; the caller
# identity was already established by PreProcessNode.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.output_contract import OUTPUT_WITHHELD_NOTICE, check_release

logger = logging.getLogger(__name__)

# Every state field that can carry released text. All of them are cleared on a
# violation: `result` because the framework's envelope falls back to it, the
# rest because a withheld response should not survive anywhere in the state a
# later step or a checkpoint could read.
_OUTPUT_BEARING_FIELDS = (
    "result",
    "formatted_response",
    "adapted_response",
    "hq_incident_record",
    "incident_history",
    "sop_results",
)


class PostProcessNode(FunctionNode):
    """Response boundary for RET-C2-253.

    The outer backbone's post_process slot. Verifies the domain workflow's
    assembled response against the output contract before it reaches the caller.

    The contract, in full:
    - a response is present and non-empty;
    - when the workflow flagged an escalation, the released response still
      carries the escalation instruction and a known escalation tier;
    - the head-office incident record, when present, is written in Japanese;
    - nothing credential-shaped is released.

    Output (partial dict — only changed keys):
        formatted_output, status, error_log, and — on a violation — the cleared
        output-bearing fields.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])
        result = state.get("result")

        ok, reason = check_release(result)
        if not ok:
            return self._withhold(error_log, reason, state)

        # check_release rejects every other shape, so this is a narrowing for the
        # type checker rather than a runtime branch.
        released: Dict[str, Any] = result if isinstance(result, dict) else {}
        escalation_flag = bool(released.get("escalation_flag", False))
        hq_record_present = bool(released.get("hq_incident_record_ja"))
        logger.info(
            "PostProcessNode: response released escalation_flag=%s hq_record_present=%s",
            escalation_flag,
            hq_record_present,
        )
        emit_trace_event(
            "post_process_output_released",
            {
                "escalation_flag": escalation_flag,
                "escalation_tier": released.get("escalation_tier"),
                "hq_record_present": hq_record_present,
                "query_intent": released.get("query_intent", "UNKNOWN"),
            },
            state,
        )

        return {
            "formatted_output": released,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }

    def _withhold(self, error_log: List[str], reason: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """Withhold the response: clear it, then report a closed-set reason.

        The reason is one of the labels in the output contract, never text built
        from the offending content — a reason assembled from the withheld
        response is how that response leaves through the error channel instead of
        the output one.
        """
        logger.warning("PostProcessNode: response withheld (%s)", reason)
        emit_trace_event("post_process_output_withheld", {"reason": reason}, state)

        withheld: Dict[str, Any] = {field: None for field in _OUTPUT_BEARING_FIELDS}
        withheld.update(
            {
                # Truthy on purpose: the envelope resolves `formatted_output or
                # result`, so a falsy replacement re-opens the fallback.
                "formatted_output": OUTPUT_WITHHELD_NOTICE,
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"PostProcessNode: response withheld ({reason})"],
            }
        )
        return withheld
