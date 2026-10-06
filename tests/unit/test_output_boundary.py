# The response boundary: what it releases, and what it withholds.
#
# The framework's envelope resolves `formatted_output or result`, and it does so
# on an error status as well as a successful one. So a boundary that returns an
# error while leaving the assembled response in `result` still ships that
# response to the caller. Every test below asserts the CLEARING, not just the
# status — a status assertion alone passes on the shipped defect.

import pytest

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import StoreManagerOpsQAAgent
from src.nodes.post_process_node import PostProcessNode
from src.services.output_contract import (
    ESCALATION_BANNER,
    ESCALATION_TIER_LABEL,
    OUTPUT_WITHHELD_NOTICE,
    check_release,
    contains_japanese,
)


@pytest.fixture(autouse=True)
def silence_trace(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _releasable(**overrides) -> dict:
    result = {
        "response_in_staff_lang": (
            "Step 1: Power-cycle the unit.\n" f"{ESCALATION_BANNER}\n{ESCALATION_TIER_LABEL} tier1_maintenance"
        ),
        "hq_incident_record_ja": "【本部報告書】種別: EQUIPMENT_FAULT",
        "escalation_flag": True,
        "escalation_tier": "tier1_maintenance",
        "query_intent": "EQUIPMENT_FAULT",
        "memory_updated": False,
    }
    result.update(overrides)
    return result


def _state(result) -> dict:
    return {"result": result, "error_log": [], "node_history": []}


def _envelope(state: dict, partial: dict) -> dict:
    """Resolve the caller-visible envelope from a state plus a node's update.

    This is the framework's own resolution, applied to the merged state — the
    only thing that decides what a caller actually receives.
    """
    merged = dict(state)
    merged.update(partial)
    return StoreManagerOpsQAAgent().get_output(merged)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_a_conforming_response_is_released() -> None:
    partial = PostProcessNode().execute(_state(_releasable()))
    assert partial["status"] == AgentStatus.SUCCESS
    assert partial["formatted_output"]["response_in_staff_lang"]


def test_a_response_without_escalation_is_released() -> None:
    partial = PostProcessNode().execute(
        _state(
            _releasable(
                escalation_flag=False,
                escalation_tier=None,
                hq_incident_record_ja=None,
                # A compliance question neither escalates nor files a record.
                query_intent="COMPLIANCE_QUERY",
                response_in_staff_lang="Refer to the Store Operations Manual.",
            )
        )
    )
    assert partial["status"] == AgentStatus.SUCCESS


# ---------------------------------------------------------------------------
# Withholding — every violation path, measured on the envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "result"),
    [
        ("not a response", None),
        ("not a mapping", "a plain string"),
        ("empty response", _releasable(response_in_staff_lang="   ")),
        (
            "escalation instruction suppressed",
            _releasable(response_in_staff_lang="Step 1: Power-cycle the unit."),
        ),
        ("escalation tier missing", _releasable(escalation_tier=None)),
        ("escalation tier unknown", _releasable(escalation_tier="tier9_unheard_of")),
        (
            "head-office record not in Japanese",
            _releasable(hq_incident_record_ja="Incident report: cooler fault"),
        ),
        (
            "head-office record missing on a class that must file one",
            _releasable(hq_incident_record_ja=None),
        ),
        (
            "credential in the released response",
            _releasable(hq_incident_record_ja="【報告】AKIAIOSFODNN7EXAMPLE"),
        ),
        (
            "credential assignment shape the framework detector does not describe",
            _releasable(hq_incident_record_ja="【報告】password=hunter2swordfish"),
        ),
    ],
)
def test_a_violating_response_is_withheld_and_cleared(label: str, result) -> None:
    """Every non-success path clears the response, not only the one a sweep names."""
    state = _state(result)
    partial = PostProcessNode().execute(state)

    assert partial["status"] == AgentStatus.ERROR, label
    # Presence AND emptiness: a missing key falls through to the merged state.
    assert "result" in partial and partial["result"] is None, label
    for field in ("formatted_response", "adapted_response", "hq_incident_record"):
        assert field in partial and partial[field] is None, label

    envelope = _envelope(state, partial)
    assert envelope["output"] == OUTPUT_WITHHELD_NOTICE, label


def test_the_withheld_envelope_carries_none_of_the_withheld_text() -> None:
    """Containment, asserted on the caller-visible envelope rather than on state."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    state = _state(_releasable(hq_incident_record_ja=f"【報告】{secret}"))
    partial = PostProcessNode().execute(state)
    envelope = _envelope(state, partial)

    serialised = str(envelope)
    assert secret not in serialised
    assert "Power-cycle" not in serialised
    assert "Traceback" not in serialised
    assert "src/" not in serialised


def test_the_replacement_notice_is_truthy() -> None:
    """A falsy replacement re-opens the envelope's fallback onto the withheld text.

    `formatted_output or result` — an empty string, an empty dict or a missing
    key all resolve to `result`, which is the response being withheld.
    """
    assert OUTPUT_WITHHELD_NOTICE
    state = _state(_releasable(response_in_staff_lang=""))
    partial = PostProcessNode().execute(state)
    assert bool(partial["formatted_output"]) is True


def test_the_withheld_reason_is_a_closed_set_label() -> None:
    """A reason built from the offending content is an output channel of its own."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    state = _state(_releasable(hq_incident_record_ja=f"【報告】{secret}"))
    partial = PostProcessNode().execute(state)
    joined = " ".join(partial["error_log"])
    assert "credential_shape_in_output" in joined
    assert secret not in joined


# ---------------------------------------------------------------------------
# The contract helpers, in both directions
# ---------------------------------------------------------------------------


def test_contains_japanese_both_ways() -> None:
    assert contains_japanese("【本部報告書】")
    assert contains_japanese("冷蔵庫が故障しました")
    assert contains_japanese("エスカレーション")
    assert not contains_japanese("Incident report: cooler fault")
    assert not contains_japanese("")
    assert not contains_japanese(None)


def test_check_release_reports_the_first_violation_only() -> None:
    ok, reason = check_release(_releasable())
    assert ok and reason == ""
    ok, reason = check_release(_releasable(response_in_staff_lang=""))
    assert not ok and reason == "response_empty"
