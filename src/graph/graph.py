"""AgentCore Platform v1.0"""

# RET-C2-253 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max_retry)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (StoreOpsGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#   src/graph/context_bridge.py        ← carries the validated request inward
#
# Class-name alignment (critical):
#   graph.py class:    StoreManagerOpsQAAgent
#   config/agent.yaml: class: "src.graph.graph.StoreManagerOpsQAAgent"
#   src/api/server.py: from src.graph.graph import StoreManagerOpsQAAgent
#   These three MUST match exactly.
#
# Rules enforced:
#   ✅ StoreManagerOpsQAAgent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ StoreOpsGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No Level 0 platform SDK imports

from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import publish_request_contract
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.runtime_config import FALLBACK_STORE_OPS, resolve_store_ops


class StoreOpsGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of StoreManagerOpsQAAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph
      extract_input()   — the question text, plus the validated contract published
                          on the context bridge for the inner graph to seed
      merge_output()    — map sub_result fields into outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    # "handle": call on_subgraph_error() instead — use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: human-review interrupts are handled inside the inner graph only.
    # True: surface an inner interrupt to the outer caller.
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph()

    def extract_input(self, state: AgentState) -> str:
        """Return the QUESTION TEXT passed into inner_graph.invoke().

        The framework carries a single string across the nested-graph boundary,
        so the string must be the thing the inner graph's first step reads: the
        question. Passing a serialized envelope here instead made the classifier
        read the envelope — and, because JSON serialization escapes non-ASCII,
        a Japanese question arrived with no Japanese in it at all and classified
        on the literal key names.

        Everything else the outer backbone validated travels on the context
        bridge, which the inner graph consumes in its initial-state hook.
        """
        validated = state.get("validated_input")
        contract: Dict[str, Any] = {}
        question = ""

        if isinstance(validated, dict):
            question = str(validated.get("query_text", ""))
            contract = {
                "validated_input": dict(validated),
                "store_id": validated.get("store_id"),
                "staff_lang": validated.get("staff_lang"),
            }
        else:
            raw = state.get("user_input", "")
            question = raw if isinstance(raw, str) else str(raw)

        # The resolved runtime block travels inward too, so a value declared in
        # config/config.yaml reaches the domain steps instead of each of them
        # falling back to its own built-in constant.
        declared = state.get("store_ops_config")
        contract["store_ops_config"] = dict(declared) if isinstance(declared, dict) else dict(FALLBACK_STORE_OPS)

        publish_request_contract(contract)
        return question

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output()   emits: "formatted_response", "status",
                                      "trace_id", "correlation_id",
                                      "node_history"
          This merge_output() reads: sub_result.get("formatted_response"),
                                     sub_result.get("status")
        """
        return {
            "result": sub_result.get("formatted_response"),
            "status": sub_result.get("status"),
        }


class StoreManagerOpsQAAgent(AgentBaseGraph):
    """Outer graph for RET-C2-253 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in StoreOpsGraphNode (main slot), which delegates
    to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY structural override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (VERIFIED_EXTERNAL — input validation)
      - main:        StoreOpsGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "ret_c2_253"

    @property
    def state_schema(self) -> type:
        return State

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the resolved runtime block into every request's state.

        `self.config` is what the registry (or the standalone entry point) passed
        as `Graph(config=...)`. Seeding the store-operations block here is what
        makes a value declared in config/config.yaml reachable by the domain
        steps: nodes receive state, never the graph, and the `config` argument on
        the node signature is never supplied by the framework's node wrapper.
        """
        declared = self.config.get("store_ops")
        resolved = resolve_store_ops(declared) if isinstance(declared, dict) else dict(FALLBACK_STORE_OPS)
        return {"store_ops_config": resolved}

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = StoreOpsGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — server.py may reference either name.
Graph = StoreManagerOpsQAAgent
