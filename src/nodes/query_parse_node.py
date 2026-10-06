"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-253 — QueryParseNode (inner DomainWorkflowGraph, step 1)
#
# Classifies the store manager query intent and extracts structured metadata
# from validated_input.  First node in the inner domain pipeline.
#
# Intent classification:
#   EQUIPMENT_FAULT   — cooler, freezer, POS, signage, refrigerator fault reports
#   INCIDENT_LOG      — incident recording requests or logging instructions
#   COMPLIANCE_QUERY  — food-loss reporting, safety compliance, 食品ロス法
#   STAFF_INSTRUCTION — procedural questions for staff training / 育成就労
#   GENERAL_OPS       — general store operations questions (catch-all)
#
# S-4: audit trace emitted on every execution.
# Trust level: ANONYMOUS — inner domain node; caller was gated by PreProcessNode.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent classification — keyword pattern sets
# ---------------------------------------------------------------------------

_EQUIPMENT_KEYWORDS = frozenset(
    {
        "cooler",
        "freezer",
        "refrigerator",
        "冷蔵",
        "冷凍",
        "pos",
        "register",
        "レジ",
        "signage",
        "display",
        "ディスプレイ",
        "hvac",
        "air conditioner",
        "エアコン",
        "noise",
        "音",
        "leak",
        "漏れ",
        "broken",
        "壊れ",
        "故障",
        "malfunction",
        "不具合",
        "fault",
        "error",
        "エラー",
        "maintenance",
        "修理",
    }
)

_INCIDENT_KEYWORDS = frozenset(
    {
        "incident",
        "インシデント",
        "report",
        "レポート",
        "log",
        "記録",
        "ログ",
        "record",
        "file",
        "document",
        "register incident",
        "事故",
        "怪我",
        "injury",
        "theft",
        "盗難",
        "complaint",
        "クレーム",
    }
)

_COMPLIANCE_KEYWORDS = frozenset(
    {
        "compliance",
        "コンプライアンス",
        "food loss",
        "食品ロス",
        "廃棄",
        "waste",
        "reporting",
        "報告",
        "checklist",
        "チェックリスト",
        "audit",
        "監査",
        "regulation",
        "規制",
        "法",
        "safety",
        "安全",
        "hygiene",
        "衛生",
    }
)

_STAFF_KEYWORDS = frozenset(
    {
        "staff",
        "スタッフ",
        "育成就労",
        "employee",
        "training",
        "研修",
        "procedure",
        "手順",
        "manual",
        "マニュアル",
        "instruction",
        "指示",
        "how to",
        "やり方",
        "guide",
        "ガイド",
        "step",
        "steps",
    }
)


def _classify_intent(query_text: str) -> str:
    """Classify query intent by keyword-overlap strength.

    Scores each intent by the number of its keywords present in the query and
    returns the highest-scoring intent (strongest signal wins). A plain
    priority short-circuit is wrong when a generic token overlaps two sets:
    e.g. "food loss compliance report" contains the generic INCIDENT token
    "report" but has two strong COMPLIANCE tokens ("food loss", "compliance"),
    so it must classify as COMPLIANCE_QUERY, not INCIDENT_LOG.

    Ties (and the all-zero case, which yields GENERAL_OPS) are broken by the
    documented priority order:
        EQUIPMENT_FAULT > INCIDENT_LOG > COMPLIANCE_QUERY > STAFF_INSTRUCTION
    """
    lower = query_text.lower()

    # Ordered by priority for deterministic tie-breaking (first wins on a tie).
    scored = [
        ("EQUIPMENT_FAULT", sum(1 for kw in _EQUIPMENT_KEYWORDS if kw in lower)),
        ("INCIDENT_LOG", sum(1 for kw in _INCIDENT_KEYWORDS if kw in lower)),
        ("COMPLIANCE_QUERY", sum(1 for kw in _COMPLIANCE_KEYWORDS if kw in lower)),
        ("STAFF_INSTRUCTION", sum(1 for kw in _STAFF_KEYWORDS if kw in lower)),
    ]

    best_intent, best_score = max(scored, key=lambda item: item[1])
    if best_score == 0:
        return "GENERAL_OPS"
    return best_intent


# Equipment type hint extraction.
#
# The Japanese terms are matched WITHOUT word boundaries on purpose. `\b` is
# computed over `\w`, which includes Kana and Kanji, so a Japanese term sitting
# between other Japanese characters never has a boundary either side — and
# Japanese is written without spaces, so that is the normal case, not the corner
# one. A single `\b(...)\b` alternation therefore matched the English equipment
# names and silently never matched the Japanese ones.
_EQUIPMENT_TYPE_RE = re.compile(
    r"\b(?:cooler|freezer|refrigerator|pos|register|signage|display|hvac|air.conditioner)\b"
    r"|冷蔵庫?|冷凍庫?|レジ|エアコン|冷ケース",
    re.IGNORECASE,
)

# Equipment terms map onto one canonical class, so a step downstream can compare
# against a single token whatever language the question was written in. Before
# this, the safety step that moves perishables to a backup unit was keyed on the
# literal English word and fired only for questions written in English.
_EQUIPMENT_CANONICAL = {
    "冷蔵": "cooler",
    "冷蔵庫": "cooler",
    "冷ケース": "cooler",
    "冷凍": "freezer",
    "冷凍庫": "freezer",
    "refrigerator": "cooler",
    "レジ": "pos",
    "register": "pos",
    "エアコン": "hvac",
    "air conditioner": "hvac",
    "air-conditioner": "hvac",
}


def _extract_incident_type_hint(query_text: str) -> Optional[str]:
    """Extract an equipment / incident type hint from the query text.

    Returns the canonical lower-case equipment class, or None if no match.
    """
    match = _EQUIPMENT_TYPE_RE.search(query_text)
    if not match:
        return None
    term = match.group(0).lower()
    return _EQUIPMENT_CANONICAL.get(term, term)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class QueryParseNode(FunctionNode):
    """Parse and classify the store manager query.

    First node in the inner DomainWorkflowGraph for RET-C2-253.

    Input state keys (seeded by the outer backbone from the validated request):
        validated_input: dict — {query_text, store_id, staff_lang}
        user_input:      str  — the question, as the fallback source

    Both are the QUESTION. Passing a serialized envelope here instead of the
    question made this step classify the envelope: JSON serialization escapes
    non-ASCII, so a Japanese question arrived with no Japanese in it and the only
    keyword left to match was a literal key name.

    Output state keys (partial dict — only changed keys):
        query_intent:       classified intent string
        incident_type_hint: optional equipment/incident type string
        out_of_scope:       bool (True if query_text is unusable)
        status:             AgentStatus
        error_log:          list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # ------------------------------------------------------------------
        # 1. Resolve validated_input from state.
        # ------------------------------------------------------------------
        validated_input = state.get("validated_input")

        if not isinstance(validated_input, dict):
            # Fallback to the raw question when no validated payload was seeded.
            raw = state.get("user_input", "")
            if not isinstance(raw, str) or not raw.strip():
                return {
                    "status": AgentStatus.ERROR,
                    "error_log": error_log
                    + [
                        "QueryParseNode: validated_input is absent and " "user_input is also empty — cannot parse query"
                    ],
                }
            validated_input = {
                "query_text": raw.strip(),
                "store_id": "UNKNOWN",
                "staff_lang": "ja",
            }

        query_text: str = str(validated_input.get("query_text", "")).strip()

        if not query_text:
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + ["QueryParseNode: query_text is empty in validated_input"],
            }

        # ------------------------------------------------------------------
        # 2. Classify intent.
        # ------------------------------------------------------------------
        query_intent = _classify_intent(query_text)

        # ------------------------------------------------------------------
        # 3. Extract equipment / incident type hint.
        # ------------------------------------------------------------------
        incident_type_hint = _extract_incident_type_hint(query_text)

        # ------------------------------------------------------------------
        # 4. S-4 audit trace.
        # ------------------------------------------------------------------
        logger.info(
            "QueryParseNode: intent=%s hint=%s store_id=%s",
            query_intent,
            incident_type_hint,
            validated_input.get("store_id", "UNKNOWN"),
        )
        emit_trace_event(
            "query_parsed",
            {
                "query_intent": query_intent,
                "incident_type_hint": incident_type_hint,
                "store_id": validated_input.get("store_id", "UNKNOWN"),
                "query_length": len(query_text),
            },
            state,
        )

        return {
            "query_intent": query_intent,
            "incident_type_hint": incident_type_hint,
            "out_of_scope": False,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
