"""The MCP server: F-58 to F-62, and §2.11's rules about references.

Driven against a fake loopback service, because what is under test is the
projection -- that each tool is one REST call, that failures arrive as
codes rather than exceptions, and that a resource reference is an
identifier and never an address.
"""

from __future__ import annotations

import json

import httpx
import pytest

from echoact.errors import Code, EchoActError
from echoact.mcp import server as mcp_server
from echoact.mcp.client import RestClient, describe
from echoact.mcp.config import Connection, from_environment

TOKEN = "eak_test_" + "a" * 40
CONNECTION = Connection(base_url="http://127.0.0.1:8765", token=TOKEN, timeout_s=5.0)

RESULT = {
    "result_id": "res_1",
    "job_id": "job_1",
    "format": "wav",
    "sample_rate": 44100,
    "frame_count": 44100,
    "byte_size": 88244,
    "digest": "a" * 64,
    "expires_at": 2_000_000_000.0,
}


class FakeService:
    """Records what was asked and answers the way §2.10 says."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, str, dict]] = []
        self.overrides = overrides

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v1", "", 1)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if path in self.overrides:
            return self.overrides[path]
        if path == "/status":
            return httpx.Response(200, json={"mcp_enabled": True, "version": "0.1.0"})
        if path == "/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "model_id": "supertonic-3",
                            "voices": [
                                {"voice_id": "F1", "display_name": "Female 1", "description": "..."}
                            ],
                        }
                    ]
                },
            )
        if path == "/estimate":
            return httpx.Response(200, json={"valid": True, "segment_count": 2, "audio_ms": 4200})
        if path == "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": "job_1", "state": "accepted"})
        if path == "/jobs":
            return httpx.Response(200, json={"items": [], "total": 0})
        if path == "/jobs/job_1":
            return httpx.Response(200, json={"job_id": "job_1", "state": "generating"})
        if path == "/jobs/job_1/cancel":
            return httpx.Response(202, json={"job_id": "job_1", "state": "canceling"})
        if path == "/jobs/job_1/segments":
            return httpx.Response(200, json={"segments": [], "last_sequence": 0})
        if path == "/jobs/job_1/result":
            return httpx.Response(200, json=RESULT)
        if path.endswith("/audio"):
            return httpx.Response(200, content=b"RIFFfake", headers={"content-type": "audio/wav"})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "No such item."})

    def client(self) -> RestClient:
        return RestClient(CONNECTION, transport=httpx.MockTransport(self.handler))


def _tool_names(mcp) -> set[str]:
    import asyncio

    return {t.name for t in asyncio.run(mcp.list_tools())}


def _call(mcp, name: str, **kw):
    import asyncio

    async def run():
        tool = await mcp.get_tool(name)
        return await tool.run(kw)

    return asyncio.run(run())


def _payload(result):
    """The structured content a tool returned."""
    data = getattr(result, "structured_content", None)
    if data is not None:
        return data.get("result", data)
    return json.loads(result.content[0].text)


# ------------------------------------------------------------ the shape ---


def test_the_tool_list_is_exactly_the_eight_the_document_names() -> None:
    """F-59: each tool maps onto one REST operation and adds no capability
    REST does not already expose. An extra tool would be one that does."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    assert _tool_names(mcp) == {
        "list_models",
        "estimate_speech",
        "create_speech",
        "get_speech_job",
        "cancel_speech_job",
        "list_speech_segments",
        "get_speech_result",
        "list_speech_history",
    }


def test_every_tool_makes_exactly_one_request() -> None:
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    for name, kw in (
        ("list_models", {}),
        ("estimate_speech", {"text": "안녕하세요."}),
        ("get_speech_job", {"job_id": "job_1"}),
        ("cancel_speech_job", {"job_id": "job_1"}),
        ("list_speech_segments", {"job_id": "job_1"}),
        ("list_speech_history", {}),
    ):
        fake.calls.clear()
        _call(mcp, name, **kw)
        assert len(fake.calls) == 1, f"{name} made {len(fake.calls)} requests"


def test_create_speech_sends_the_kind_and_the_key() -> None:
    """F-54 requires an explicit job kind; F-49 requires the key."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    _call(mcp, "create_speech", text="안녕하세요.", idempotency_key="k-1")
    _method, path, body = fake.calls[-1]
    assert path == "/jobs"
    assert body["kind"] == "speech"
    assert body["idempotency_key"] == "k-1"
    assert body["retention"] == "one_off"


def test_a_setting_the_caller_did_not_name_is_not_sent() -> None:
    """F-47 has every entry path share the owner's choices. Sending a
    default from here would override the owner instead of deferring."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    _call(mcp, "create_speech", text="안녕하세요.", idempotency_key="k")
    _m, _p, body = fake.calls[-1]
    assert "voice_id" not in body
    assert "tempo" not in body
    assert "language" not in body


def test_a_wait_longer_than_the_ceiling_is_clamped() -> None:
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    _call(mcp, "create_speech", text="hi", idempotency_key="k", wait_seconds=9999)
    _m, _p, body = fake.calls[-1]
    assert body["wait_s"] == 60.0


# ------------------------------------------------------- the references ---


def test_a_result_reference_is_an_identifier_not_an_address() -> None:
    """§2.11: never a local file path, never a URL carrying a token."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    payload = _payload(_call(mcp, "get_speech_result", job_id="job_1"))
    uri = payload["resource"]["uri"]
    assert uri == "echoact://job/job_1/audio"
    assert TOKEN not in uri
    assert "http" not in uri
    assert ":\\" not in uri and not uri.startswith("/")


def test_a_reference_is_private_and_no_fresher_than_the_result() -> None:
    """F-60: a freshness hint no longer than the remaining lifetime, and
    private, so no shared intermediary caches one owner's audio."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    payload = _payload(_call(mcp, "get_speech_result", job_id="job_1"))
    resource = payload["resource"]
    assert resource["cache_scope"] == "private"
    remaining_ms = (RESULT["expires_at"] - __import__("time").time()) * 1000
    assert 0 <= resource["ttl_ms"] <= max(0, remaining_ms) + 1000


def test_an_expired_result_gets_no_freshness_at_all() -> None:
    assert mcp_server._ttl_ms({"expires_at": 100.0}, now=200.0) == 0


def test_a_retained_result_is_still_not_cached_forever() -> None:
    """No expiry does not mean no staleness: the owner may delete it."""
    assert mcp_server._ttl_ms({"expires_at": None}, now=0.0) == 3_600_000


def test_the_audio_itself_is_not_in_the_tool_answer() -> None:
    """F-60: large audio is not embedded in the default tool response."""
    fake = FakeService()
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    payload = _payload(_call(mcp, "get_speech_result", job_id="job_1"))
    assert "audio" not in payload
    assert "blob" not in payload
    assert not any(isinstance(v, (bytes, bytearray)) for v in payload.values())


# ----------------------------------------------------------- the failures ---


def test_a_refused_connection_is_reported_as_the_app_not_running() -> None:
    """F-52 wants this distinct and non-retrying, and forbids this process
    from starting the app."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    rest = RestClient(CONNECTION, transport=httpx.MockTransport(refuse))
    with pytest.raises(EchoActError) as caught:
        rest.get("/status")
    assert caught.value.code is Code.APP_NOT_RUNNING
    assert "cannot start it" in caught.value.message


def test_mcp_disabled_is_its_own_error() -> None:
    fake = FakeService(**{"/status": httpx.Response(200, json={"mcp_enabled": False})})
    with pytest.raises(EchoActError) as caught:
        mcp_server.preflight(fake.client())
    assert caught.value.code is Code.MCP_DISABLED
    assert "cannot enable it" in caught.value.message


def test_the_services_own_error_code_crosses_unchanged() -> None:
    """N-24 makes a behaviour difference between REST and MCP a defect, so
    the code is carried across rather than re-derived."""
    fake = FakeService(
        **{
            "/jobs": httpx.Response(
                409,
                json={"code": "BUSY", "message": "A generation job is already running.",
                      "retryable": True, "retry_after_s": 3.0, "request_id": "req_1"},
            )
        }
    )
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    payload = _payload(_call(mcp, "create_speech", text="hi", idempotency_key="k"))
    assert payload["error"]["code"] == "BUSY"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["retry_after_s"] == 3.0


def test_a_terminal_condition_is_not_offered_as_retryable() -> None:
    err = EchoActError(Code.APP_NOT_RUNNING)
    assert describe(err)["error"]["retryable"] is False


def test_a_tool_failure_is_data_rather_than_an_exception() -> None:
    """F-57 promises a code and whether a retry can succeed. An exception
    gives the client neither."""
    fake = FakeService(**{"/models": httpx.Response(401, json={"code": "CREDENTIAL_EXPIRED"})})
    mcp = mcp_server.build(CONNECTION, client=fake.client())
    payload = _payload(_call(mcp, "list_models"))
    assert payload["error"]["code"] == "CREDENTIAL_EXPIRED"
    assert payload["error"]["retryable"] is False


# ------------------------------------------------------------- the config ---


def test_a_missing_credential_refuses_to_start(monkeypatch) -> None:
    """N-31 fails closed, and it reaches here too."""
    monkeypatch.delenv("ECHOACT_TOKEN", raising=False)
    with pytest.raises(EchoActError) as caught:
        from_environment([])
    assert caught.value.code is Code.UNAUTHENTICATED


def test_a_non_loopback_url_is_refused(monkeypatch) -> None:
    """N-17 says EchoAct is never reachable off this machine, so a URL
    pointing elsewhere is a misconfiguration, not an address to try."""
    monkeypatch.setenv("ECHOACT_TOKEN", TOKEN)
    with pytest.raises(EchoActError) as caught:
        from_environment(["--url", "http://192.168.1.10:8765"])
    assert caught.value.code is Code.HOST_NOT_ALLOWED


def test_the_credential_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("ECHOACT_TOKEN", TOKEN)
    monkeypatch.delenv("ECHOACT_URL", raising=False)
    connection = from_environment([])
    assert connection.token == TOKEN
    assert connection.base_url == "http://127.0.0.1:8765"
    assert connection.api_root.endswith("/api/v1")
    assert connection.headers()["Authorization"] == f"Bearer {TOKEN}"


def test_the_token_never_appears_in_a_url(monkeypatch) -> None:
    """N-17: tokens are never included in URLs."""
    monkeypatch.setenv("ECHOACT_TOKEN", TOKEN)
    connection = from_environment([])
    assert TOKEN not in connection.api_root
    assert TOKEN not in connection.base_url
