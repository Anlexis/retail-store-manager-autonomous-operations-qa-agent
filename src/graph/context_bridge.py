"""AgentCore Platform v1.0"""

# RET-C2-253 — carrying the validated request across the nested-graph boundary.
#
# The framework hands a nested graph exactly one thing: a string. The node that
# wraps the inner graph receives the outer state, but `subgraph.invoke()` is
# called with `user_input` alone — no state, no request context. Everything the
# outer backbone validated (the store identifier, the staff language, the
# resolved runtime block) therefore stops at the boundary unless something
# carries it across.
#
# What that cost before this bridge existed, measured on the shipped code:
#
#   * the wrapper passed the validated payload across as a JSON string, so the
#     inner graph's first step classified the JSON ENVELOPE instead of the
#     question. `json.dumps` escapes non-ASCII, so a Japanese question arrived
#     as `\uXXXX` sequences with no Japanese left in it, and the only keyword
#     the classifier could still see was the literal key name `staff_lang` —
#     every Japanese question classified as a staff-training request. A
#     Japanese equipment-fault report produced no escalation and no head-office
#     record at all;
#   * the store identifier never arrived, so the per-store incident lookup ran
#     against `UNKNOWN` for every store, and its documented per-store isolation
#     was vacuous rather than enforced;
#   * the staff language never arrived, so the response was never adapted and
#     the multilingual path — the reason this agent exists for non-Japanese
#     staff — never ran.
#
# A ContextVar is the right carrier because both graphs run in the same thread
# within one request: the wrapper publishes as it extracts the input, and the
# inner graph consumes it in its own initial-state hook, before its first node.
# Consuming CLEARS it, so one request's contract cannot be read twice or survive
# into another. The outer wrapper publishes on every request — an empty mapping
# included — so a value left behind by a failed invocation is overwritten rather
# than inherited.

from contextvars import ContextVar
from typing import Any, Dict

_EMPTY: Dict[str, Any] = {}

# Published by the outer wrapper's extract_input(); consumed by the inner
# graph's _extra_initial_state(). The default empty mapping keeps a direct
# inner-graph invocation working — it simply gets no seeded contract, which is
# the same degraded path as a request that supplied no context.
_REQUEST_CONTRACT: ContextVar[Dict[str, Any]] = ContextVar("ret_c2_253_request_contract", default=_EMPTY)


def publish_request_contract(contract: Dict[str, Any]) -> None:
    """Publish the validated contract for the inner graph to pick up."""
    _REQUEST_CONTRACT.set(dict(contract))


def consume_request_contract() -> Dict[str, Any]:
    """Return the published contract and clear it.

    Clearing on read is what bounds the value to a single request: nothing can
    read it a second time, and a later direct invocation of the inner graph gets
    the documented empty mapping rather than a previous caller's store.
    """
    contract = _REQUEST_CONTRACT.get()
    _REQUEST_CONTRACT.set(_EMPTY)
    return dict(contract)
