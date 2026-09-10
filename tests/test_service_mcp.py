"""REST and MCP against each other, over a real loopback socket.

N-24 makes the REST contract canonical and MCP a projection of it, and a
behaviour difference between the two a defect.  Every other test of
either side uses a fake for the other, which is exactly the arrangement
that lets a difference hide.  So this starts the real service on a real
port and drives the real MCP server at it.

No model is loaded and nothing is synthesised: the seam under test is the
contract, and F-88's estimate exists precisely so a caller can exercise
validation without creating a job.
"""

from __future__ import annotations

import socket
import time

import pytest

from echoact import paths
from echoact.domain import Capability
from echoact.errors import Code, EchoActError
from echoact.mcp import server as mcp_server
from echoact.mcp.client import RestClient
from echoact.mcp.config import Connection


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def running(tmp_path, monkeypatch):
    """The application with its service actually listening."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from echoact.app import Application

    app = Application()
    port = _free_port()
    app.update_settings(rest_enabled=True, rest_port=port, mcp_enabled=True)

    problem = app.start_service()
    if problem is not None:
        app.shutdown()
        paths.data_dir.cache_clear()
        pytest.skip(f"the service did not start: {problem.code}")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not app.service_running:
        time.sleep(0.05)

    issued = app.credentials.issue(
        name="integration test",
        capabilities={Capability.GENERATE, Capability.READ_RESULTS},
        days=1,
    )
    connection = Connection(
        base_url=f"http://127.0.0.1:{port}", token=issued.token, timeout_s=10.0
    )
    try:
        yield app, connection
    finally:
        app.shutdown()
        paths.data_dir.cache_clear()


# ------------------------------------------------------------- the seam ---


def test_the_mcp_server_can_talk_to_the_real_service(running) -> None:
    _app, connection = running
    with RestClient(connection) as rest:
        status = rest.get("/status")
    assert status
    assert "version" in status or "service" in status


def test_preflight_passes_when_the_owner_enabled_mcp(running) -> None:
    _app, connection = running
    with RestClient(connection) as rest:
        assert mcp_server.preflight(rest) is not None


def test_preflight_refuses_when_the_owner_disabled_mcp(running) -> None:
    """F-52 wants this distinct and non-retrying."""
    app, connection = running
    app.update_settings(mcp_enabled=False)
    with RestClient(connection) as rest:
        with pytest.raises(EchoActError) as caught:
            mcp_server.preflight(rest)
    assert caught.value.code is Code.MCP_DISABLED


def test_a_tool_reaches_the_real_service(running) -> None:
    _app, connection = running
    with RestClient(connection) as rest:
        mcp = mcp_server.build(connection, client=rest)
        payload = _payload(_call(mcp, "list_models"))
    assert "error" not in payload, payload
    assert payload


def test_the_estimate_agrees_across_both_surfaces(running) -> None:
    """The same text, the same answer, whichever way it is asked. That is
    what N-24 means by MCP being a projection."""
    _app, connection = running
    text = "에코액트는 문서를 소리내어 읽어 줍니다. 두 번째 문장입니다."
    with RestClient(connection) as rest:
        direct = rest.post("/estimate", {"text": text})
        mcp = mcp_server.build(connection, client=rest)
        through_mcp = _payload(_call(mcp, "estimate_speech", text=text))
    assert "error" not in through_mcp, through_mcp
    assert through_mcp.get("segment_count") == direct.get("segment_count")
    assert through_mcp.get("audio_ms") == direct.get("audio_ms")


# ------------------------------------------------------------- security ---


def test_no_credential_is_refused(running) -> None:
    """N-31: fail closed, including on a service that is on by default."""
    import httpx

    _app, connection = running
    response = httpx.get(f"{connection.api_root}/status", timeout=5.0)
    assert response.status_code == 401


def test_a_wrong_credential_is_refused(running) -> None:
    import httpx

    _app, connection = running
    response = httpx.get(
        f"{connection.api_root}/status",
        headers={"Authorization": "Bearer eak_not_a_real_credential"},
        timeout=5.0,
    )
    assert response.status_code == 401


def test_an_unexpected_host_is_refused_before_authentication(running) -> None:
    """N-17 fixes the order: Host and Origin are validated before the
    credential is looked at. A valid credential must not rescue a request
    that claimed the wrong host."""
    import httpx

    _app, connection = running
    response = httpx.get(
        f"{connection.api_root}/status",
        headers={"Host": "evil.example.com", **{"Authorization": f"Bearer {connection.token}"}},
        timeout=5.0,
    )
    assert response.status_code == 400, response.text


def test_the_service_listens_only_on_loopback(running) -> None:
    """N-17: binding a non-loopback address is not configurable, so there
    should be nothing answering on one."""
    import httpx

    _app, connection = running
    port = connection.base_url.rsplit(":", 1)[1]
    host = _lan_address()
    if host is None:
        pytest.skip("no non-loopback address on this machine")
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        httpx.get(f"http://{host}:{port}/api/v1/status", timeout=2.0)


def _lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


# ------------------------------------------------------------- helpers ---


def _call(mcp, name: str, **kw):
    import asyncio

    async def run():
        tool = await mcp.get_tool(name)
        return await tool.run(kw)

    return asyncio.run(run())


def _payload(result):
    import json

    data = getattr(result, "structured_content", None)
    if data is not None:
        return data.get("result", data)
    return json.loads(result.content[0].text)
