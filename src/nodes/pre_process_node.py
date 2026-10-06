"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — PreProcessNode (outer backbone, pre_process slot)
#
# The node that owns the caller contract. It validates the store manager's
# question, resolves the store identifier and the staff language, and writes the
# structured payload the domain workflow consumes.
#
# The screens are enforced HERE, in the node, not only in the HTTP adapter: when
# this agent runs behind the platform gateway the adapter is not in the path at
# all, so a guarantee that lives only there is a guarantee that is absent in
# production. The adapter applies the same screens from the same module so the
# two cannot drift.
#
# Trust level: VERIFIED_EXTERNAL — this is the external-facing entry. Only
# callers with a verified external identity may invoke the agent.
#
# Input state keys:
#   user_input:    str  — raw store manager question
#   input_context: dict — optional, already reduced to the accepted inert subset
#
# Output state keys (partial dict — only changed keys):
#   validated_input: dict  — {query_text, store_id, staff_lang}
#   store_id:        str   — validated store identifier
#   staff_lang:      str   — staff language code (ja/en/vi/tl)
#   out_of_scope:    bool
#   status:          AgentStatus
#   error_log:       list[str]

import logging
import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.caller_contract import (
    MAX_QUERY_CHARS,
    SUPPORTED_LANGUAGES,
    detect_credential_shape,
    detect_injection,
    validate_context,
)
from src.services.runtime_config import FALLBACK_STORE_OPS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Store identifier used when the caller supplies none and the question embeds
# none. It is a sentinel, not a store: the incident lookup treats it as "no
# store scope" rather than as a namespace to read.
UNKNOWN_STORE: str = "UNKNOWN"

# Simple pattern to extract store_id from a question string.
# Matches "store:STORE-001" or a leading "[STORE-001]".
_STORE_ID_RE = re.compile(
    r"(?:store[:\s]+|^\[)([A-Z0-9][A-Z0-9\-_]{1,31})(?:\])?",
    re.IGNORECASE,
)

# Matches a language-preference hint, e.g. "lang:vi" or "[lang=tl]".
_LANG_RE = re.compile(r"(?:lang[=:\s]+)([a-z]{2})", re.IGNORECASE)

# Minimum meaningful question length (characters).
MIN_QUERY_CHARS: int = 5


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class PreProcessNode(FunctionNode):
    """Input boundary for RET-C2-253.

    The outer backbone's pre_process slot. Validates and structures the raw
    store manager question before the domain workflow begins.

    Validation rules:
    - user_input must be a string of at least MIN_QUERY_CHARS and at most
      MAX_QUERY_CHARS characters.
    - The question is screened for disallowed instruction patterns, raw and
      markup-stripped, and for credential shapes. A refusal names neither the
      matched text nor the value.
    - store_id and staff_lang come from the request context when the caller
      supplied one, and otherwise from hints embedded in the question. The
      context wins: it is validated structurally, while a hint is whatever the
      platform's personal-data mask left behind.
    - An unsupported language code falls back to the declared default.

    Output (partial dict — only changed keys):
        validated_input, store_id, staff_lang, out_of_scope, status, error_log.
    """

    # External-facing entry: require a verified external caller identity.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])
        store_ops = state.get("store_ops_config")
        if not isinstance(store_ops, dict):
            store_ops = dict(FALLBACK_STORE_OPS)
        default_language = str(store_ops.get("default_language", "ja"))
        if default_language not in SUPPORTED_LANGUAGES:
            default_language = "ja"

        # ------------------------------------------------------------------
        # 1. Resolve and bound the question.
        # ------------------------------------------------------------------
        user_input = state.get("user_input", "")
        if not isinstance(user_input, str):
            user_input = str(user_input)

        query_text = user_input.strip()

        if len(query_text) < MIN_QUERY_CHARS:
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "question_too_short", "length": len(query_text)},
                state,
            )
            return self._reject(
                error_log,
                f"user_input is empty or too short (minimum {MIN_QUERY_CHARS} characters)",
            )

        if len(query_text) > MAX_QUERY_CHARS:
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "question_too_long", "length": len(query_text)},
                state,
            )
            return self._reject(
                error_log,
                f"user_input exceeds the {MAX_QUERY_CHARS}-character limit",
            )

        # ------------------------------------------------------------------
        # 2. Screen the question before any of it is trusted.
        # ------------------------------------------------------------------
        if detect_injection(query_text) is not None:
            # The matched marker is NOT reported: it is caller text, and echoing
            # it back is how a refusal becomes an output channel.
            emit_trace_event(
                "pre_process_injection_blocked",
                {"reason": "disallowed_instruction_pattern"},
                state,
            )
            return self._reject(error_log, "user_input contains a disallowed instruction pattern")

        if detect_credential_shape(query_text):
            emit_trace_event(
                "pre_process_credential_blocked",
                {"reason": "credential_shape_in_question"},
                state,
            )
            return self._reject(error_log, "user_input contains a credential-shaped value")

        # ------------------------------------------------------------------
        # 3. Resolve store_id and staff_lang.
        #    The request context wins over a hint embedded in the question: the
        #    context is validated structurally at the boundary, whereas the
        #    question has already been through the platform's personal-data mask
        #    and may have had an identifier rewritten out of it.
        # ------------------------------------------------------------------
        context, refusals = validate_context(state.get("input_context"))
        if refusals:
            reason, field = refusals[0]
            emit_trace_event(
                "pre_process_rejected",
                {"reason": "request_context_rejected", "field": field},
                state,
            )
            return self._reject(error_log, f"input_context.{field} rejected: {reason}")

        store_id: str = context.get("store_id") or UNKNOWN_STORE
        if store_id == UNKNOWN_STORE:
            store_match = _STORE_ID_RE.search(query_text)
            if store_match:
                store_id = store_match.group(1).upper()

        staff_lang: str = context.get("staff_lang") or ""
        if not staff_lang:
            lang_match = _LANG_RE.search(query_text)
            if lang_match:
                candidate = lang_match.group(1).lower()
                if candidate in SUPPORTED_LANGUAGES:
                    staff_lang = candidate
                else:
                    logger.info(
                        "PreProcessNode: unsupported language hint — using the declared default '%s'",
                        default_language,
                    )
        if not staff_lang:
            staff_lang = default_language

        # ------------------------------------------------------------------
        # 4. Build the structured payload the domain workflow consumes.
        # ------------------------------------------------------------------
        validated_input: Dict[str, Any] = {
            "query_text": query_text,
            "store_id": store_id,
            "staff_lang": staff_lang,
        }

        logger.info(
            "PreProcessNode: validated question store_id=%s staff_lang=%s query_len=%d",
            store_id,
            staff_lang,
            len(query_text),
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "store_id": store_id,
                "staff_lang": staff_lang,
                "store_id_source": "request_context" if context.get("store_id") else "question",
                "query_length": len(query_text),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "store_id": store_id,
            "staff_lang": staff_lang,
            "out_of_scope": False,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }

    @staticmethod
    def _reject(error_log: List[str], reason: str) -> Dict[str, Any]:
        """Refuse the request, naming the field and never the value."""
        return {
            "status": AgentStatus.ERROR,
            "error_log": error_log + [f"PreProcessNode: {reason}"],
        }
