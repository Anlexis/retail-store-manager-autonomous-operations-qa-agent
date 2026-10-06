# A declared runtime value must change what the agent releases.
#
# Config that nobody reads is the failure mode this file exists to close, and it
# is invisible from the inside: the manifest validates, the suite passes, CI goes
# green, and every declared value is dead. So these tests do not assert that a
# value was loaded — they drive the compiled graph twice with different
# declarations and assert the RELEASED response differs.
#
# The path a value takes is longer than it looks. Nodes receive state, never the
# graph, and the framework's node wrapper never supplies the optional `config`
# argument on the node signature. So a declared value reaches a domain step only
# via: Graph(config=...) -> _extra_initial_state() -> outer state ->
# the context bridge -> the inner graph's own _extra_initial_state() -> state.
# Any break in that chain leaves the declaration dead.

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import StoreManagerOpsQAAgent

_FAULT = "store:STORE-042 The cooler in Aisle 3 is making a loud noise again. " "What is the escalation procedure?"


def _run(config: dict, user_input: str = _FAULT, **invoke_kwargs) -> dict:
    agent = StoreManagerOpsQAAgent(config=config)
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(user_input=user_input, ctx=ctx, **invoke_kwargs)


def _store_ops(**overrides) -> dict:
    block = {
        "default_language": "ja",
        "escalation_history_threshold": 1,
        "max_incident_entries": 5,
    }
    block.update(overrides)
    return {"max_retry": 3, "timeout_s": 30, "store_ops": block}


def test_the_escalation_threshold_changes_the_released_tier() -> None:
    """The declared threshold decides which tier a recurrent fault reaches.

    Both runs read the same store history; only the declaration differs. If the
    value never arrived, both would report the same tier.
    """
    low = _run(_store_ops(escalation_history_threshold=1))["output"]
    high = _run(_store_ops(escalation_history_threshold=9))["output"]

    assert low["escalation_tier"] == "tier2_hq"
    assert high["escalation_tier"] == "tier1_maintenance"
    assert low["hq_incident_record_ja"] != high["hq_incident_record_ja"]
    assert "tier2_hq" in low["hq_incident_record_ja"]
    assert "tier1_maintenance" in high["hq_incident_record_ja"]


def test_the_incident_entry_cap_changes_the_released_summary() -> None:
    """The declared cap bounds how much history one response summarises."""
    capped = _run(_store_ops(max_incident_entries=1))["output"]
    uncapped = _run(_store_ops(max_incident_entries=5))["output"]

    assert "1 similar incident(s)" in capped["response_in_staff_lang"]
    assert "2 similar incident(s)" in uncapped["response_in_staff_lang"]


def test_the_cap_of_zero_removes_the_history_section_entirely() -> None:
    none_at_all = _run(_store_ops(max_incident_entries=0))["output"]
    assert "similar incident(s)" not in none_at_all["response_in_staff_lang"]
    # With no escalated history, the fault is not recurrent.
    assert none_at_all["escalation_tier"] == "tier1_maintenance"


def test_the_declared_default_language_changes_the_released_response() -> None:
    """With no language on the request, the declared default decides the output."""
    japanese = _run(_store_ops(default_language="ja"))["output"]
    vietnamese = _run(_store_ops(default_language="vi"))["output"]

    assert "[Tiếng Việt]" not in japanese["response_in_staff_lang"]
    assert "[Tiếng Việt]" in vietnamese["response_in_staff_lang"]


def test_a_request_supplied_language_wins_over_the_declared_default() -> None:
    output = _run(
        _store_ops(default_language="ja"),
        input_context={"staff_lang": "en"},
    )["output"]
    assert "[English]" in output["response_in_staff_lang"]


@pytest.mark.parametrize("unusable", ["NaN", float("inf"), 0, 10**6, True])
def test_an_unusable_declaration_degrades_rather_than_silently_changing_behaviour(
    unusable,
) -> None:
    """A NaN threshold makes every comparison False — fail CLOSED, not open.

    Left unguarded, `prior_escalated >= NaN` is False for every store, so every
    recurrent fault silently drops to the lower escalation tier.
    """
    degraded = _run(_store_ops(escalation_history_threshold=unusable))["output"]
    documented = _run(_store_ops(escalation_history_threshold=1))["output"]
    assert degraded["escalation_tier"] == documented["escalation_tier"] == "tier2_hq"


def test_a_graph_built_with_no_config_still_runs_on_the_documented_defaults() -> None:
    """A caller that constructs the graph bare gets the shipped behaviour."""
    bare = _run({})["output"]
    declared = _run(_store_ops())["output"]
    assert bare["escalation_tier"] == declared["escalation_tier"]


# ---------------------------------------------------------------------------
# The context bridge itself
# ---------------------------------------------------------------------------


def test_the_store_identifier_reaches_the_incident_lookup() -> None:
    """An unscoped request reads no history; a scoped one does.

    The identifier does not cross the nested-graph boundary as an argument —
    the framework carries only a string — so this is the assertion that the
    bridge is live rather than decorative.
    """
    scoped = _run(_store_ops())["output"]
    unscoped = _run(
        _store_ops(),
        user_input="The cooler in Aisle 3 is making a loud noise again. What do I do?",
    )["output"]

    assert "similar incident(s)" in scoped["response_in_staff_lang"]
    assert "similar incident(s)" not in unscoped["response_in_staff_lang"]


def test_the_bridge_does_not_leak_between_requests() -> None:
    """One request's store scope must not be inherited by the next.

    The bridge clears on read, and the wrapper publishes on every request, so a
    scoped run followed by an unscoped one gets no history the second time.
    """
    _run(_store_ops())
    unscoped = _run(
        _store_ops(),
        user_input="The cooler in Aisle 3 is making a loud noise again. What do I do?",
    )["output"]
    assert "similar incident(s)" not in unscoped["response_in_staff_lang"]


def test_a_japanese_fault_report_reaches_the_equipment_path() -> None:
    """The question crosses the boundary as the question, not as an envelope."""
    output = _run(
        _store_ops(),
        user_input="store:STORE-042 冷蔵庫が故障して異音がします。エスカレーション手順を教えてください。",
    )["output"]
    assert output["query_intent"] == "EQUIPMENT_FAULT"
    assert output["hq_incident_record_ja"]
    # The equipment class is recognised in Japanese too: `\b` is computed over
    # `\w`, which includes Kanji, so a boundary-anchored pattern never matched a
    # Japanese term sitting between other Japanese characters.
    assert "機器種別: cooler" in output["hq_incident_record_ja"]
