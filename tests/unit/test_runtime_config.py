# Runtime configuration: declared values reach the code, and unusable ones do not.
#
# The failure this guards against is silent in both directions. A reader pointed
# at the wrong file finds no key, falls back to its own default, and every
# declared value is dead while the suite stays green. And a NaN threshold parses
# through `float()` while every comparison against it is False — so a recurrent
# equipment fault would quietly stop reaching the higher escalation tier, with
# nothing reporting an error.

import math

import pytest

from src.services.runtime_config import (
    FALLBACK_AGENT,
    FALLBACK_STORE_OPS,
    _finite_in_range,
    agent_config,
    load_runtime_config,
    resolve_store_ops,
    store_ops_config,
)


# ---------------------------------------------------------------------------
# The shipped file
# ---------------------------------------------------------------------------


def test_the_shipped_config_is_readable() -> None:
    declared = load_runtime_config()
    assert declared, "config/config.yaml did not load — every declared value would be dead"
    assert "store_ops" in declared


def test_every_shipped_key_survives_the_bounds_check() -> None:
    """A shipped value the loader rejects is a shipped value nothing uses."""
    declared = load_runtime_config()
    resolved = agent_config()
    assert resolved["max_retry"] == declared["max_retry"]
    assert resolved["timeout_s"] == declared["timeout_s"]
    for key, value in declared["store_ops"].items():
        assert resolved["store_ops"][key] == value, key


def test_agent_config_carries_the_store_ops_block() -> None:
    resolved = agent_config()
    assert set(resolved) == {"max_retry", "timeout_s", "store_ops"}
    assert set(resolved["store_ops"]) == set(FALLBACK_STORE_OPS)


# ---------------------------------------------------------------------------
# The finite + bounded parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        None,
        "not a number",
        "",
        [],
        {},
        10**9,
        -1,
    ],
)
def test_an_unusable_number_is_rejected(value) -> None:
    assert _finite_in_range(value, 0, 100) is None


@pytest.mark.parametrize("value", [0, 1, 50, 100, "42", 3.5])
def test_a_usable_number_is_accepted(value) -> None:
    parsed = _finite_in_range(value, 0, 100)
    assert parsed is not None and math.isfinite(parsed)


# ---------------------------------------------------------------------------
# Per-field bounds, fail-closed to the documented default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("escalation_history_threshold", "NaN"),
        ("escalation_history_threshold", float("inf")),
        ("escalation_history_threshold", 0),
        ("escalation_history_threshold", 999),
        ("escalation_history_threshold", True),
        ("escalation_history_threshold", "many"),
        ("max_incident_entries", "NaN"),
        ("max_incident_entries", -1),
        ("max_incident_entries", 10**6),
        ("max_incident_entries", None),
        ("default_language", "zz"),
        ("default_language", 42),
        ("default_language", None),
    ],
)
def test_an_out_of_range_declaration_degrades_to_the_default(field: str, bad) -> None:
    resolved = resolve_store_ops({field: bad})
    assert resolved[field] == FALLBACK_STORE_OPS[field], (field, bad)


@pytest.mark.parametrize(
    ("field", "good", "expected"),
    [
        ("escalation_history_threshold", 3, 3),
        ("escalation_history_threshold", "2", 2),
        ("max_incident_entries", 0, 0),
        ("max_incident_entries", 12, 12),
        ("default_language", "vi", "vi"),
        ("default_language", "en", "en"),
    ],
)
def test_an_in_range_declaration_is_honoured(field: str, good, expected) -> None:
    assert resolve_store_ops({field: good})[field] == expected


def test_a_missing_block_degrades_to_the_documented_defaults() -> None:
    assert resolve_store_ops({}) == FALLBACK_STORE_OPS


def test_the_fallbacks_mirror_the_shipped_file() -> None:
    """An unreadable config must degrade to the documented behaviour.

    Fallbacks that drifted from the shipped file would make an exotic deployment
    behave differently from every other one, silently.
    """
    declared = load_runtime_config()
    assert FALLBACK_AGENT["max_retry"] == declared["max_retry"]
    assert FALLBACK_AGENT["timeout_s"] == declared["timeout_s"]
    assert FALLBACK_STORE_OPS == declared["store_ops"]
    assert store_ops_config() == declared["store_ops"]
