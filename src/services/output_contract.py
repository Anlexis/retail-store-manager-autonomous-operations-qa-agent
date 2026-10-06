"""AgentCore Platform v1.0"""

# RET-C2-253 — the output contract the response boundary enforces.
#
# The documented promise of this agent is narrow and safety-shaped:
#
#   1. a response is always present;
#   2. when the workflow flags an escalation, the released response still
#      CARRIES the escalation instruction — a store manager must never be told
#      "power-cycle the unit" with the "call the contractor" half removed;
#   3. an equipment fault or a logged incident always files a head-office
#      record, and that record is written in Japanese whatever language the
#      staff member reads, because it is filed centrally;
#   4. nothing credential-shaped leaves the agent.
#
# The markers and the checks live here, in one module, so the step that WRITES
# the escalation instruction and the boundary that VERIFIES it cannot drift
# apart — a boundary that greps for its own private copy of a string passes
# happily the day the renderer changes wording.
#
# This template renders no monetary aggregates: the released response is SOP
# procedure text, an escalation tier from a closed set, counts, and the Japanese
# record. There is therefore no rounding grid to enforce; the invariants above
# are what the boundary checks instead.

import re
from typing import Any, FrozenSet, Tuple

from src.services.caller_contract import detect_credential_shape

# The escalation instruction, as the synthesis step writes it and as the output
# boundary looks for it.
ESCALATION_BANNER: str = "*** ESCALATION REQUIRED ***"
ESCALATION_TIER_LABEL: str = "Escalation tier:"

# Escalation tiers the workflow may emit. A tier outside this set is a defect in
# the synthesis step, not a caller condition, and the boundary refuses rather
# than releasing an instruction nobody can act on.
ESCALATION_TIERS: FrozenSet[str] = frozenset({"tier1_maintenance", "tier2_hq", "emergency"})

# Replacement released in place of a withheld response. It must be TRUTHY: the
# framework's envelope resolves `formatted_output or result`, so an empty string
# here would re-open the fallback onto the very text being withheld.
OUTPUT_WITHHELD_NOTICE: str = (
    "The response was withheld because it did not satisfy this agent's output contract. "
    "No partial result is released. Retry the request; if it recurs, raise it with head office."
)

# Reasons the boundary may report, as a closed set. A free-text reason built
# from the offending content is how the withheld text leaks out through the
# error channel instead of the output one.
REASON_NOT_A_RESPONSE = "response_missing"
REASON_EMPTY_RESPONSE = "response_empty"
REASON_ESCALATION_SUPPRESSED = "escalation_suppressed"
REASON_ESCALATION_TIER_UNKNOWN = "escalation_tier_unknown"
REASON_RECORD_MISSING = "hq_record_missing"
REASON_RECORD_NOT_JAPANESE = "hq_record_not_japanese"
REASON_CREDENTIAL_SHAPE = "credential_shape_in_output"

# Question classes that must file a head-office record. This was documented as
# an output invariant from the start and never actually checked.
RECORD_REQUIRED_INTENTS: FrozenSet[str] = frozenset({"EQUIPMENT_FAULT", "INCIDENT_LOG"})

# Hiragana, Katakana, CJK ideographs and the CJK punctuation the record uses.
_JAPANESE_RE = re.compile(r"[぀-ゟ゠-ヿ㐀-䶿一-鿿가-힯、-〿]")


def contains_japanese(text: Any) -> bool:
    """True when *text* carries at least one Japanese character."""
    return isinstance(text, str) and bool(_JAPANESE_RE.search(text))


def escalation_instruction_present(response_text: Any) -> bool:
    """True when the released response still carries the escalation instruction."""
    if not isinstance(response_text, str):
        return False
    return ESCALATION_BANNER in response_text or ESCALATION_TIER_LABEL in response_text


def check_release(result: Any) -> Tuple[bool, str]:
    """Verify one assembled response against the output contract.

    Returns ``(ok, reason)``; *reason* is one of the closed-set labels above and
    is empty when the response may be released. The caller is responsible for
    CLEARING the output-bearing state on a failure — returning an error status
    while leaving the assembled response in state still releases it, because the
    framework's envelope falls back to it.
    """
    if not isinstance(result, dict):
        return False, REASON_NOT_A_RESPONSE

    response_text = result.get("response_in_staff_lang")
    if not isinstance(response_text, str) or not response_text.strip():
        return False, REASON_EMPTY_RESPONSE

    if result.get("escalation_flag"):
        tier = result.get("escalation_tier")
        if not tier or tier not in ESCALATION_TIERS:
            return False, REASON_ESCALATION_TIER_UNKNOWN
        if not escalation_instruction_present(response_text):
            return False, REASON_ESCALATION_SUPPRESSED

    record = result.get("hq_incident_record_ja")
    if record is None:
        if result.get("query_intent") in RECORD_REQUIRED_INTENTS:
            return False, REASON_RECORD_MISSING
    elif not isinstance(record, str) or not record.strip() or not contains_japanese(record):
        return False, REASON_RECORD_NOT_JAPANESE

    # The credential screen is the union of the framework's detector and this
    # template's assignment shapes. A narrower local set would let the framework
    # raise INSIDE the boundary node, and the framework's own error handler then
    # discards this node's update — including the clearing — so a detector gap
    # is a containment bypass rather than a weaker filter.
    if detect_credential_shape(result):
        return False, REASON_CREDENTIAL_SHAPE

    return True, ""
