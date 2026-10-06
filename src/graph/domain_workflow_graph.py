"""AgentCore Platform v1.0"""

# RET-C2-253 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full store manager ops Q&A domain workflow:
#
#   START → query_parse → incident_memory_recall → sop_kb_search
#         → escalation_synthesize → multilingual_adapt → response_format → END
#
# Called by StoreOpsGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with StoreOpsGraphNode.merge_output()
#   ✅ All domain nodes use NO constructor args (SDK-v1 convention)
#   ❌ No Level 0 platform SDK imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import consume_request_contract
from src.nodes.escalation_synthesize_node import EscalationSynthesizeNode
from src.nodes.incident_memory_recall_node import IncidentMemoryRecallNode
from src.nodes.multilingual_adapt_node import MultilingualAdaptNode
from src.nodes.query_parse_node import QueryParseNode
from src.nodes.response_format_node import ResponseFormatNode
from src.nodes.sop_kb_search_node import SOPKBSearchNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-253.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by StoreOpsGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → query_parse             (QueryParseNode)
          → incident_memory_recall  (IncidentMemoryRecallNode)
          → sop_kb_search           (SOPKBSearchNode)
          → escalation_synthesize   (EscalationSynthesizeNode)
          → multilingual_adapt      (MultilingualAdaptNode)
          → response_format         (ResponseFormatNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_253_domain_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        No mandatory config keys: everything this graph needs arrives in its
        initial state, seeded by _extra_initial_state() below. Each domain step
        still carries a documented fallback, so a request that reaches the inner
        graph directly behaves like one that supplied no request context.
        """
        pass

    # ── Request contract seeding ──────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed what the outer backbone validated into this graph's state.

        The framework carries a single string across the nested-graph boundary,
        so the store identifier, the staff language and the resolved runtime
        block cannot arrive as arguments. The outer wrapper publishes them on the
        context bridge as it extracts the question; this hook consumes them
        before the first step runs.

        Only keys that are actually present are returned, so a direct invocation
        of this graph (no outer wrapper, nothing published) starts from the
        documented defaults instead of a wall of None.
        """
        contract = consume_request_contract()
        seeded: Dict[str, Any] = {}
        for key in ("validated_input", "store_id", "staff_lang", "store_ops_config"):
            value = contract.get(key)
            if value is not None:
                seeded[key] = value
        return seeded

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 6 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        All nodes instantiated with NO constructor args (SDK-v1 convention).
        """
        self._nodes["query_parse"] = QueryParseNode()
        self._nodes["incident_memory_recall"] = IncidentMemoryRecallNode()
        self._nodes["sop_kb_search"] = SOPKBSearchNode()
        self._nodes["escalation_synthesize"] = EscalationSynthesizeNode()
        self._nodes["multilingual_adapt"] = MultilingualAdaptNode()
        self._nodes["response_format"] = ResponseFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear store ops Q&A domain topology.

        Each step passes its partial-dict output into the shared State.
        The topology is intentionally linear — no conditional branching
        between domain nodes.  route() is implemented as required by the ABC
        but add_conditional_edges() is not used.

        Flow:
          query_parse → incident_memory_recall → sop_kb_search
          → escalation_synthesize → multilingual_adapt → response_format
        """
        self._sg.add_edge(START, "query_parse")
        self._sg.add_edge("query_parse", "incident_memory_recall")
        self._sg.add_edge("incident_memory_recall", "sop_kb_search")
        self._sg.add_edge("sop_kb_search", "escalation_synthesize")
        self._sg.add_edge("escalation_synthesize", "multilingual_adapt")
        self._sg.add_edge("multilingual_adapt", "response_format")
        self._sg.add_edge("response_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime.  It is implemented to satisfy the
        ABC contract.  Returns END on error so an unexpected call does not
        re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "response_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by StoreOpsGraphNode.merge_output() in graph.py
        as the `sub_result` argument.  Both methods are designed together to
        guarantee field-name consistency:

            Inner get_output()   emits: "formatted_response", "status",
                                        "trace_id", "correlation_id",
                                        "node_history"
            Outer merge_output() reads: sub_result.get("formatted_response"),
                                        sub_result.get("status")

        Additional fields are surfaced for observability / downstream extension.
        """
        return {
            "formatted_response": state.get("formatted_response"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def get_state_class(self) -> type:
        """Return the State TypedDict used by both inner and outer graphs."""
        return State
