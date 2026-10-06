# The deploy payload is part of the entry contract, and nothing else checks it.
#
# `deploy/invoke_payload.json` is posted verbatim by the deployment sign-off
# step. That step asserts the HTTP response was well-formed — it does not assert
# the agent ANSWERED. So a payload the entry contract refuses still produces a
# 200 with a well-formed body, every surrounding assertion passes, and the only
# record is a failure line inside an artifact nobody opens.
#
# The payload here is built from the same fixture the contract tests use, so the
# payload the deployment posts and the payload the tests assert on cannot drift
# apart.

import json
import os
from pathlib import Path

import pytest

from tests.integration.test_invoke_contract import _BASE_REQUEST

_PAYLOAD_PATH = Path(__file__).resolve().parents[2] / "deploy" / "invoke_payload.json"
_TOKEN = "deploy-payload-suite-token-0123456789"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN
    from src.api.server import app

    with TestClient(app) as test_client:
        yield test_client
    os.environ.pop("INVOKE_AUTH_TOKEN", None)


def _payload() -> dict:
    return json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))


def test_the_payload_exists_and_is_an_object() -> None:
    payload = _payload()
    assert isinstance(payload, dict)
    assert set(payload) <= {"input", "session_id", "input_context"}


def test_the_payload_question_comes_from_the_suite_fixture() -> None:
    """Same request object, so the deploy and the tests cannot assert different contracts."""
    assert _payload()["input"] == _BASE_REQUEST["input"]


def test_the_deploy_payload_is_answered_not_merely_accepted(client) -> None:
    """The whole point: a 200 is not an answer.

    The deployment step checks the HTTP status and that the body parses. Both
    hold for a request the agent refuses. This asserts the agent produced a real
    domain response.
    """
    response = client.post("/invoke", json=_payload(), headers={"Authorization": f"Bearer {_TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success", body.get("error_log")

    output = body["output"]
    assert isinstance(output, dict)
    assert output["response_in_staff_lang"].strip()
    assert output["query_intent"] == "EQUIPMENT_FAULT"


def test_a_payload_the_entry_contract_refuses_would_still_return_200(client) -> None:
    """Verify the verifier: prove the check above can actually fail.

    A probe that only ever sees the good case cannot distinguish "the payload is
    right" from "the assertion is wrong".
    """
    response = client.post(
        "/invoke",
        json={"input": "hi", "session_id": "probe"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "error"
