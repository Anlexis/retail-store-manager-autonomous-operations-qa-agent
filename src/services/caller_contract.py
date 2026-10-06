"""AgentCore Platform v1.0"""

# RET-C2-253 — the caller-data contract.
#
# Two caller channels reach this agent and both are screened here, so the
# adapter and the node that owns the contract cannot drift apart:
#
#   1. the request string  — the store manager's question, free text;
#   2. the request context — a closed set of inert identifiers (`store_id`,
#      `staff_lang`) that the platform's personal-data mask does not cover.
#
# Why the context channel is a closed set of identifiers rather than free text:
# the framework returns the whole context channel verbatim inside the first
# node's own result, and the output gate then scans every value of every result.
# A credential-shaped string anywhere on that channel therefore fails the run at
# the FIRST node, before any of this template's code runs, with a traceback the
# caller cannot act on. A channel restricted to inert identifiers cannot carry
# one — and the screen below refuses the request readably if a caller tries.
#
# Every refusal names the FIELD and never echoes the value. Field names are
# caller data too, so a name is echoed only when it is itself inert.

import re
import unicodedata
from typing import Any, Dict, FrozenSet, List, Optional, Pattern, Tuple

from framework.security.credential_detector import detect_credentials_in_value

# ---------------------------------------------------------------------------
# Structural caps
# ---------------------------------------------------------------------------

# Upper bound on the question, applied before any parsing. Store-manager
# questions are a sentence or two; anything larger is a payload, not a question.
MAX_QUERY_CHARS: int = 4000

# Upper bound on the serialized request context. Every accepted field is
# separately bounded below; this cap stops a caller spending the adapter's
# parsing budget on a context it was never going to keep.
MAX_CONTEXT_BYTES: int = 4096

# The context channel is a closed set. An undeclared key is refused rather than
# ignored: ignoring is not stripping, and an ignored key still rides the context
# channel into the framework's own scan.
ACCEPTED_CONTEXT_KEYS: Tuple[str, ...] = ("store_id", "staff_lang")

# Store identifiers as they are rendered and used as the memory namespace key.
STORE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

# Staff languages the response adaptation supports.
SUPPORTED_LANGUAGES: FrozenSet[str] = frozenset({"ja", "en", "vi", "tl"})

# A field NAME is echoed back to the caller only when it matches this.
_SAFE_FIELD_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


# ---------------------------------------------------------------------------
# Injection screen
# ---------------------------------------------------------------------------

# Markers that would never appear in a legitimate store-operations question
# (equipment fault, incident log, compliance, staff instruction), so the
# false-positive rate stays low. Each is anchored on a phrase or a structural
# token rather than a bare keyword: an unanchored verb would refuse real work —
# "insert into the logbook" and "act as the duty manager" are ordinary sentences
# in this domain.
_INJECTION_PATTERNS: Tuple[Pattern[str], ...] = (
    # Prompt-injection phrases.
    re.compile(r"ignore\s+(?:all\s+|the\s+)?(?:previous|prior|preceding|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?)", re.I),
    re.compile(r"forget\s+(?:all\s+|everything\s+)?(?:previous|prior|above|you\s+were\s+told)", re.I),
    re.compile(
        r"(?:reveal|show|print|repeat|expose|leak)\s+(?:me\s+)?(?:your\s+|the\s+)?(?:system\s+)?(?:prompt|instructions?)",
        re.I,
    ),
    re.compile(r"system\s+prompt", re.I),
    re.compile(
        r"you\s+are\s+now\s+(?:a\s+|an\s+|the\s+)?"
        r"(?:dan\b|jailbroken|unrestricted|unfiltered|developer|admin|root|different\s+ai|new\s+ai|no\s+longer)",
        re.I,
    ),
    re.compile(r"developer\s+mode", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"override\s+(?:your\s+|the\s+|all\s+)?(?:previous\s+|system\s+)?instructions?", re.I),
    # Chat-template control tokens, screened as a CLASS rather than as a list of
    # the few role names in use today. The framework's own detector scores
    # `<<SYS>>` and `<|system|>` as no finding while blocking `<|im_start|>`, so
    # a phrase-only screen misses exactly the form an attacker reaches for.
    re.compile(r"<\|[^|>]{0,64}\|>"),
    re.compile(r"<<\s*/?\s*sys\s*>>", re.I),
    re.compile(r"\[/?\s*INST\s*\]", re.I),
    # Statement-shaped SQL injection — verb pairs and a statement separator,
    # never a bare keyword.
    re.compile(r"\bunion\s+(?:all\s+)?select\b", re.I),
    re.compile(r"\b(?:drop|truncate)\s+table\b", re.I),
    re.compile(r"\bdelete\s+from\s+[\w.\"'`\[]", re.I),
    re.compile(r"\binsert\s+into\s+[\w.\"'`\[]+\s*(?:\(|values\b|select\b)", re.I),
    re.compile(r";\s*(?:drop|delete|update|insert|select)\b", re.I),
    re.compile(r"(?:\bor\b|\band\b)\s+['\"]?\d+['\"]?\s*=\s*['\"]?\d+", re.I),
)

# Markup that a naive strip would remove, taking a control token with it and
# leaving readable directive text behind — a detectable token attack converted
# into an undetectable plain-text one. Every string is therefore screened BOTH
# raw and after this strip: tokens are caught before it, directives spliced
# across markup (`ig<b>nore all previous instructions`) are caught after it.
_MARKUP_RE = re.compile(r"<[^<>]{0,120}>")

# Zero-width and directional formatting characters, which can be inserted
# between the letters of a directive to defeat a phrase match.
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def _candidates(text: str) -> Tuple[str, ...]:
    """Every form of *text* the screen runs against.

    NFKC folds full-width and other compatibility forms onto their ASCII
    equivalents, so a full-width bracket cannot smuggle a control token past a
    pattern written in ASCII.

    Invisible formatting characters are then resolved BOTH ways, because the two
    evasions they enable are opposites:
      - dropped, which reassembles a directive broken up mid-word
        (`ig<zwsp>nore all previous instructions`);
      - turned into a space, which restores the word breaks in a directive whose
        spaces were REPLACED by them
        (`ignore<zwsp>all<zwsp>previous<zwsp>instructions`).
    Resolving only one of the two leaves the other open.

    Each form is then screened raw and markup-stripped: a markup strip can turn a
    detectable control token into readable directive text, so the token must be
    caught before the strip and a directive spliced across tags after it.
    """
    normalised = unicodedata.normalize("NFKC", text)
    forms = {
        _INVISIBLE_RE.sub("", normalised),
        _INVISIBLE_RE.sub(" ", normalised),
    }
    return tuple(form for base in forms for form in (base, _MARKUP_RE.sub("", base)))


def detect_injection(text: str) -> Optional[str]:
    """Return the matched marker when *text* carries an injection attempt.

    Returns None when the text is clean.
    """
    if not isinstance(text, str) or not text:
        return None
    for candidate in _candidates(text):
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(candidate)
            if match:
                return match.group(0)
    return None


# ---------------------------------------------------------------------------
# Credential screen
# ---------------------------------------------------------------------------

# The framework's own detector is the FLOOR, never the replacement. Its patterns
# describe credential FORMATS (sk-…, eyJ…, AKIA…, Bearer …, db URIs) and match
# none of the assignment shapes below, so delegating to it alone would make this
# screen narrower while looking like a tightening. The union is what ships.
_LOCAL_CREDENTIAL_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S{4,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|apikey|secret|token|client[_-]?secret)\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\bglpat-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)-----BEGIN(?:\s+[A-Z]+)*\s+PRIVATE KEY-----"),
)


def detect_credential_shape(value: Any) -> bool:
    """True when *value* carries a credential shape.

    Union of the framework's detector — so this template's block set can never be
    narrower than the framework's, which is what makes a gap a containment bypass
    rather than a weaker filter — and the assignment shapes the framework does
    not describe.
    """
    if detect_credentials_in_value(value):
        return True
    return _local_credential_hit(value)


def _local_credential_hit(value: Any) -> bool:
    """Recurse the same shapes the framework detector walks: dicts and sequences."""
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _LOCAL_CREDENTIAL_PATTERNS)
    if isinstance(value, dict):
        return any(_local_credential_hit(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_local_credential_hit(item) for item in value)
    return False


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


def _echo_name(name: Any, position: int) -> str:
    """Render a field name for a refusal message, or a positional stand-in.

    A field name is caller data. It is echoed only when it is itself inert and
    trips no credential pattern of its own; otherwise the refusal is positional.
    """
    if isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name) and not detect_credential_shape(name):
        return name
    return f"field #{position}"


# Keys the platform itself puts into input_context, not the caller. The Marketplace
# runner invokes every agent as
#     agent.invoke(message, ctx=ctx, input_context={"conversation_history": history})
# (agenticstar-agentcore, shared/bootstrap/marketplace_app.py), whatever the user typed.
# Refusing it as an unknown field refused every chat request before the question was
# read. Discarded, not validated: nothing in this pipeline reads prior turns, and
# screening a transcript would let one earlier message refuse every later one. Discarding
# adds no exposure — the backbone's first node has already copied the raw input_context
# into state before this contract runs.
PLATFORM_RESERVED_KEYS = frozenset({"conversation_history"})


def validate_context(raw: Any) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Reduce a request context to the accepted, inert subset.

    Returns ``(accepted, refusals)``. ``refusals`` is a list of
    ``(reason, field)`` pairs; the caller refuses on the first one. An empty or
    absent context is accepted as ``{}`` — the agent degrades to the identifiers
    it can read out of the question itself.
    """
    refusals: List[Tuple[str, str]] = []
    if raw is None:
        return {}, refusals
    if not isinstance(raw, dict):
        return {}, [("request context must be an object", "input_context")]

    # Size first: everything below walks the structure, and the cap exists so a
    # caller cannot make that walk the expensive part of the request.
    if len(repr(raw).encode("utf-8", "replace")) > MAX_CONTEXT_BYTES:
        return {}, [(f"request context exceeds {MAX_CONTEXT_BYTES} bytes", "input_context")]
    raw = {k: v for k, v in raw.items() if k not in PLATFORM_RESERVED_KEYS}

    accepted: Dict[str, str] = {}
    for position, (key, value) in enumerate(raw.items(), start=1):
        name = _echo_name(key, position)

        # Credentials first, so the more specific refusal wins over
        # "not an accepted field" for an undeclared key carrying one.
        if detect_credential_shape(value) or detect_credential_shape(key):
            refusals.append(("value looks like a credential", name))
            continue
        marker = detect_injection(str(key)) or (detect_injection(value) if isinstance(value, str) else None)
        if marker is not None:
            refusals.append(("value contains a disallowed instruction pattern", name))
            continue
        if key not in ACCEPTED_CONTEXT_KEYS:
            refusals.append(("not an accepted request-context field", name))
            continue
        if not isinstance(value, str):
            refusals.append(("must be a string", name))
            continue

        if key == "store_id":
            if not STORE_ID_RE.match(value):
                refusals.append(("must be 1-32 characters of [A-Za-z0-9_-]", name))
                continue
            accepted[key] = value.upper()
        else:  # staff_lang
            if value.lower() not in SUPPORTED_LANGUAGES:
                refusals.append(
                    ("must be one of " + ", ".join(sorted(SUPPORTED_LANGUAGES)), name),
                )
                continue
            accepted[key] = value.lower()

    return accepted, refusals
