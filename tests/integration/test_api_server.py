"""API-adapter integration tests — entry auth + HITL resume ownership.

These exercise src/api/server.py over real HTTP (FastAPI TestClient), NOT the
graph directly. They are the layer that caught two defects the graph-level
tests could not:

  1. Entry auth: the old adapter defaulted unauthenticated callers to
     VERIFIED_EXTERNAL (fail-open). It must 401 without a valid INVOKE_AUTH_TOKEN.
  2. HITL resume: InvocationContext mints a RANDOM thread_id per instance, so a
     resume built from session_id alone targeted a nonexistent checkpoint and
     never reached the waiting interrupt(). The client MUST echo thread_id back,
     and a foreign session_id/caller must be rejected (fail-closed ownership).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

_TOKEN = "test-invoke-token-abc123"

_PAYLOAD = {
    "input": (
        '{"job_category": "engineer", "phase": "post_interview", '
        '"job_experience_text": "Led the backend team and built the payment service", '
        '"interview_response_text": "I designed the retry and idempotency logic"}'
    ),
    "session_id": "sess-owner",
    "job_category": "engineer",
    "phase": "post_interview",
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "stg-co")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "stg-proj")
    # Re-import so module-level app picks up the env (fresh checkpointer per test).
    import src.api.server as server

    importlib.reload(server)
    return TestClient(server.app), server


def _auth(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_invoke_without_token_is_401(client):
    c, _ = client
    r = c.post("/invoke", json=_PAYLOAD)
    assert r.status_code == 401


def test_invoke_unauthenticated_when_token_unset_is_401(monkeypatch):
    """With INVOKE_AUTH_TOKEN unset, an unauthenticated caller (no
    verified middleware trust) must get 401, not 403 — the entry-auth contract
    is 'unauthenticated → 401'."""
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    import importlib

    import src.api.server as server

    importlib.reload(server)
    r = TestClient(server.app).post("/invoke", json=_PAYLOAD)
    assert r.status_code == 401


def test_invoke_explicit_anonymous_middleware_is_401(monkeypatch):
    """A middleware that explicitly stamps ANONYMOUS must be
    treated as unauthenticated (401), not accepted merely because request.state
    .trust_level is set. Test the trust VALUE, not attribute presence."""
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    import importlib

    from framework.schemas.trust_level import TrustLevel

    import src.api.server as server

    importlib.reload(server)

    @server.app.middleware("http")
    async def _stamp_anonymous(request, call_next):
        request.state.trust_level = TrustLevel.ANONYMOUS
        return await call_next(request)

    r = TestClient(server.app).post("/invoke", json=_PAYLOAD)
    assert r.status_code == 401


def test_resume_checkpoint_without_session_id_is_fail_closed(client):
    """A checkpoint that records no session_id must NOT be resumable
    by an arbitrary authenticated caller (fail-closed ownership)."""
    c, server = client
    th = c.post("/invoke", json=_PAYLOAD, headers=_auth()).json()["thread_id"]
    # Simulate a checkpoint whose owner session was never recorded.
    orig = server.agent._compiled.get_state

    class _Snap:
        values = {"session_id": None, "caller_id": ""}

    server.agent._compiled.get_state = lambda cfg: _Snap()
    try:
        r = c.post(
            "/invoke",
            json={"input": "", "session_id": "anyone", "thread_id": th, "resume": {"action": "approve"}},
            headers=_auth(),
        )
    finally:
        server.agent._compiled.get_state = orig
    assert r.status_code == 403


def test_invoke_with_wrong_token_is_401(client):
    c, _ = client
    r = c.post("/invoke", json=_PAYLOAD, headers=_auth("nope"))
    assert r.status_code == 401


def test_invoke_with_token_suspends_at_hitl(client):
    c, _ = client
    r = c.post("/invoke", json=_PAYLOAD, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "awaiting_human"
    assert body["thread_id"]


def test_resume_requires_thread_id(client):
    c, _ = client
    c.post("/invoke", json=_PAYLOAD, headers=_auth())
    r = c.post(
        "/invoke",
        json={"input": "", "session_id": "sess-owner", "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_resume_foreign_session_is_rejected(client):
    c, _ = client
    th = c.post("/invoke", json=_PAYLOAD, headers=_auth()).json()["thread_id"]
    r = c.post(
        "/invoke",
        json={"input": "", "session_id": "attacker", "thread_id": th, "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert r.status_code == 403


def test_resume_unknown_thread_is_not_found(client):
    c, _ = client
    c.post("/invoke", json=_PAYLOAD, headers=_auth())
    r = c.post(
        "/invoke",
        json={"input": "", "session_id": "ghost", "thread_id": "00000000-dead-beef-0000-000000000000",
              "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert r.status_code == 404


def test_resume_correct_owner_reaches_success(client):
    c, _ = client
    th = c.post("/invoke", json=_PAYLOAD, headers=_auth()).json()["thread_id"]
    r = c.post(
        "/invoke",
        json={"input": "", "session_id": "sess-owner", "thread_id": th, "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body.get("output")


def test_resume_replay_after_completion_is_409(client):
    """A completed thread must not be resumable again — the pending
    gate (empty snapshot.next) rejects a replayed resume."""
    c, _ = client
    th = c.post("/invoke", json=_PAYLOAD, headers=_auth()).json()["thread_id"]
    first = c.post(
        "/invoke",
        json={"input": "", "session_id": "sess-owner", "thread_id": th, "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert first.status_code == 200 and first.json()["status"] == "success"
    replay = c.post(
        "/invoke",
        json={"input": "", "session_id": "sess-owner", "thread_id": th, "resume": {"action": "approve"}},
        headers=_auth(),
    )
    assert replay.status_code == 409
