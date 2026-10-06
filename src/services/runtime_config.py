"""AgentCore Platform v1.0"""

# RET-C2-253 — runtime configuration loader.
#
# config/agent.yaml is the static manifest: identity, entry point, trust level
# and the compile-time `requires` gates. It carries no runtime values. Everything
# tunable at runtime — max_retry, timeout_s and the `store_ops` block — lives in
# config/config.yaml and is read from here.
#
# One loader, so there is exactly one answer to "where does this value come
# from". A reader pointed at the manifest instead would not fail: it would find
# no such key, fall back to its own default, and every declared value in
# config/config.yaml would be dead while the tests stayed green.
#
# Every numeric read here goes through a finite + bounded parser. `float()`
# accepts "NaN" and "Infinity", and every comparison against NaN is False — a
# NaN escalation threshold would make `prior_escalated >= threshold` False for
# every store, so a recurrent equipment fault would silently never reach the
# higher escalation tier and nothing would report an error.

from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from framework.utils.config_loader import load_config

# src/services/runtime_config.py -> parents[2] = repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"

# Bounds for the declared values. A value outside these bounds is a deployment
# mistake, not a runtime condition: the loader falls back to the documented
# default rather than letting an out-of-range value change what staff are told.
_MAX_RETRY_MIN, _MAX_RETRY_MAX = 0, 9
_TIMEOUT_MIN, _TIMEOUT_MAX = 1, 600
_ESCALATION_THRESHOLD_MIN, _ESCALATION_THRESHOLD_MAX = 1, 20
_INCIDENT_ENTRIES_MIN, _INCIDENT_ENTRIES_MAX = 0, 50

# Languages the response adaptation supports. A declared default outside this
# set is rejected rather than passed through, because an unsupported code would
# silently fall through to the primary language at the adaptation step.
SUPPORTED_LANGUAGES: FrozenSet[str] = frozenset({"ja", "en", "vi", "tl"})

# Last-resort values, used only when config/config.yaml cannot be read at all
# (an exotic deployment layout). They mirror the shipped file, so an unreadable
# config degrades to the documented behaviour rather than to an empty mapping.
FALLBACK_STORE_OPS: Dict[str, Any] = {
    "default_language": "ja",
    "escalation_history_threshold": 1,
    "max_incident_entries": 5,
}
FALLBACK_AGENT: Dict[str, Any] = {
    "max_retry": 3,
    "timeout_s": 30,
}


def _finite_in_range(value: Any, low: float, high: float) -> Optional[float]:
    """Return *value* as a finite float inside [low, high], else None.

    Rejects bools (``isinstance(True, int)`` is True in Python), non-numeric
    strings, NaN and +/-Infinity. Fails CLOSED: the caller substitutes the
    documented default rather than proceeding with an unusable number.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN fails both comparisons; Infinity fails one. Neither can pass.
    if not (low <= number <= high):
        return None
    return number


def load_runtime_config() -> Dict[str, Any]:
    """Read config/config.yaml. Returns {} only if it is genuinely unreadable."""
    try:
        loaded = load_config(str(_CONFIG_PATH))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def store_ops_config() -> Dict[str, Any]:
    """The declared `store_ops` block, bounds-checked — never an empty mapping."""
    declared = load_runtime_config().get("store_ops")
    if not isinstance(declared, dict):
        return dict(FALLBACK_STORE_OPS)
    return resolve_store_ops(declared)


def resolve_store_ops(declared: Dict[str, Any]) -> Dict[str, Any]:
    """Bounds-check one `store_ops` mapping against the documented defaults.

    Split out from :func:`store_ops_config` so the inner graph can re-resolve a
    block that reached it through state without re-reading the file, and so the
    bounds are exercised directly in tests.
    """
    resolved = dict(FALLBACK_STORE_OPS)

    language = declared.get("default_language")
    if isinstance(language, str) and language in SUPPORTED_LANGUAGES:
        resolved["default_language"] = language

    threshold = _finite_in_range(
        declared.get("escalation_history_threshold"),
        _ESCALATION_THRESHOLD_MIN,
        _ESCALATION_THRESHOLD_MAX,
    )
    if threshold is not None:
        resolved["escalation_history_threshold"] = int(threshold)

    entries = _finite_in_range(
        declared.get("max_incident_entries"),
        _INCIDENT_ENTRIES_MIN,
        _INCIDENT_ENTRIES_MAX,
    )
    if entries is not None:
        resolved["max_incident_entries"] = int(entries)

    return resolved


def agent_config() -> Dict[str, Any]:
    """The graph-level runtime block, plus the store-operations tuning.

    This is what the entry point passes as ``Graph(config=...)``, so the values
    declared in config/config.yaml actually reach the framework backbone instead
    of it falling back to its own built-in defaults. ``max_retry`` is read by the
    backbone's retry route; ``timeout_s`` is the declared per-call budget carried
    on the graph config for the hosting runtime; the ``store_ops`` block travels
    into the domain workflow through the graph's initial state.
    """
    declared = load_runtime_config()
    merged: Dict[str, Any] = dict(FALLBACK_AGENT)

    max_retry = _finite_in_range(declared.get("max_retry"), _MAX_RETRY_MIN, _MAX_RETRY_MAX)
    if max_retry is not None:
        merged["max_retry"] = int(max_retry)
    timeout_s = _finite_in_range(declared.get("timeout_s"), _TIMEOUT_MIN, _TIMEOUT_MAX)
    if timeout_s is not None:
        merged["timeout_s"] = int(timeout_s)

    merged["store_ops"] = store_ops_config()
    return merged
