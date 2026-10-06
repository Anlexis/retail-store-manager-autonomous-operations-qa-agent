"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
#
# Entry points are adapters only — no business logic here. When the agent runs
# behind the platform gateway instead, the gateway calls agent.invoke() directly
# and this module is not in the path at all, which is why every guarantee below
# is ALSO enforced by the node that owns the caller contract. The two apply the
# same screens from src/services/caller_contract.py so they cannot drift.

import os
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import StoreManagerOpsQAAgent
from src.services.caller_contract import MAX_QUERY_CHARS, validate_context
from src.services.runtime_config import agent_config

app = FastAPI(title="Agent")

# The declared runtime values reach the backbone here. Constructing the graph
# with no config would leave every value in config/config.yaml unread, with the
# framework quietly substituting its own built-in defaults — and the domain
# steps falling back to their own constants.
agent = StoreManagerOpsQAAgent(config=agent_config())
agent.compile()
# The secret provider is scoped by the identity the manifest declares, so a
# secret resolves from the same place here and under the registry. The provider
# reads `env/namespaces/{namespace}/…` and `env/agents/{namespace}/{name}/…`,
# so a namespace that disagrees with `config/agent.yaml` silently splits one
# agent's secrets across two stores — with no boot-time symptom, because a
# missing tier file is ignored by design.
# `namespace` is lower(industry) — "ret" — not the lowercased Template ID.
# tests/integration/test_manifest_identity_alignment.py holds these two values
# to the manifest; it reads both sides rather than restating them.
agent.provision_secrets(secrets_factory(namespace="ret", agent_name="StoreManagerOpsQAAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Optional request context. Supported keys: `store_id`, `staff_lang`.
    # Anything else is refused rather than ignored — see the note in invoke().
    input_context: Optional[Dict[str, Any]] = None


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Caller authentication for the standalone deployment. The manifest declares
    # VERIFIED_EXTERNAL, and the input boundary requires it — so without this the
    # deployed agent answers `status: error` to every request it receives,
    # whoever sends it, because no caller can ever be more than anonymous.
    #
    # When INVOKE_AUTH_TOKEN is set on the server environment, a caller that no
    # upstream middleware vouched for (still ANONYMOUS) must present it as a
    # Bearer token, and then runs as VERIFIED_EXTERNAL. Trust established by
    # middleware is never demoted. This is a deployment-level caller credential
    # rather than an agent secret, so the secrets provider does not apply: no
    # invocation context exists yet at this point.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    if len(req.input) > MAX_QUERY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Rejected input: exceeds the {MAX_QUERY_CHARS}-character limit",
        )

    # Reduce the request context to the supported, inert subset BEFORE the graph
    # is invoked. Two things make this the right place for it:
    #   - an unsupported key is not merely ignored downstream. It stays on the
    #     context channel, the framework's first node returns that channel
    #     verbatim inside its own result, and the framework's output scan then
    #     fails the whole run with an error the caller cannot act on;
    #   - a credential-shaped value fails the same way. The request cannot
    #     succeed either way, so refuse it here with the field named.
    # 400 rather than 422: the validation layer owns 422 and answers there with
    # a list of error objects, so reusing it makes client handling ambiguous.
    # The field name is echoed only when it is itself inert; otherwise the
    # refusal is positional. The value is never echoed.
    accepted_context, refusals = validate_context(req.input_context)
    if refusals:
        reason, field = refusals[0]
        raise HTTPException(status_code=400, detail=f"Rejected input_context.{field}: {reason}")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        envelope: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=accepted_context)
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "StoreManagerOpsQAAgent"}
