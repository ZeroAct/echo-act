"""F-57's promise that the code says what actually happened.

The status was already right for these cases; the code was a reused last
resort, so a client switching on it was told something untrue -- that an
unreadable body was a missing job kind, or that a misspelt field was an
unknown model. A status is for the transport and a code is for the
caller, and only one of them was doing its job.

These live apart from tests/test_service.py because that file belongs to
the area that built the service, and this is the correction to it.
"""

from __future__ import annotations

import pytest

from echoact import paths
from echoact.errors import Code, http_status, is_retryable


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    from fastapi.testclient import TestClient

    from echoact.app import Application
    from echoact.domain import Capability
    from echoact.service.app import create_app
    from echoact.service.deps import ServiceContext

    app = Application()
    issued = app.credentials.issue(
        name="codes test", capabilities={Capability.OWNER}, days=1
    )
    context = ServiceContext(app, port=8765)
    api = create_app(context)
    with TestClient(
        api, base_url="http://127.0.0.1:8765", raise_server_exceptions=False
    ) as test_client:
        test_client.headers["Authorization"] = f"Bearer {issued.token}"
        try:
            yield test_client
        finally:
            context.close()
            app.shutdown()
            paths.data_dir.cache_clear()


def _voice() -> dict:
    return {
        "model_id": "supertonic-3",
        "voice_id": "F1",
        "gender": "female",
        "language": "auto",
        "style": "natural",
        "tempo": 1.0,
    }


# ---------------------------------------------------------- the catalogue ---


def test_the_new_codes_carry_the_statuses_the_contract_fixes() -> None:
    assert http_status(Code.MALFORMED_REQUEST) == 400
    assert http_status(Code.UNKNOWN_OPTION) == 400
    assert http_status(Code.VOICE_SETTINGS_INVALID) == 422
    # None of the three can be fixed by waiting, so none may invite a retry.
    for code in (Code.MALFORMED_REQUEST, Code.UNKNOWN_OPTION, Code.VOICE_SETTINGS_INVALID):
        assert not is_retryable(code)


# ------------------------------------------------------------- on the wire ---


def test_an_unreadable_body_says_so_rather_than_blaming_the_job_kind(client) -> None:
    response = client.post(
        "/api/v1/jobs",
        content=b"{not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == Code.MALFORMED_REQUEST.value
    assert response.json()["retryable"] is False


def test_a_misspelt_field_is_named_as_an_unknown_option(client) -> None:
    """F-54 refuses an unknown option rather than dropping it, and the
    caller cannot look the difference up in a document -- so the code and
    the detail have to name it."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "kind": "speech",
            "idempotency_key": "k",
            "text": "안녕하세요.",
            "voice": _voice(),
            "retainn": True,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == Code.UNKNOWN_OPTION.value
    assert "retainn" in body["detail"]["unknown_fields"]
    assert "retainn" in body["message"]


def test_a_misspelt_voice_option_is_an_invalid_voice_setting(client) -> None:
    """Section 2.10 fixes 422 for the voice settings, and the code now
    says what the fault is rather than borrowing 'unknown model'."""
    voice = _voice() | {"speed": 2.0}
    response = client.post(
        "/api/v1/estimate", json={"kind": "speech", "text": "안녕하세요.", "voice": voice}
    )
    assert response.status_code == 422
    assert response.json()["code"] == Code.VOICE_SETTINGS_INVALID.value


def test_a_malformed_multipart_body_is_the_clients_fault_not_the_servers(client) -> None:
    """A 500 tells a client the server broke and that retrying may help.
    Neither is true of a body it encoded wrongly."""
    response = client.post(
        "/api/v1/jobs",
        content=b"--boundary\r\nnonsense without headers\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=boundary"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == Code.MALFORMED_REQUEST.value


def test_a_refusal_still_creates_no_job(client) -> None:
    client.post("/api/v1/jobs", content=b"{bad", headers={"Content-Type": "application/json"})
    listed = client.get("/api/v1/jobs")
    assert listed.status_code == 200
    assert listed.json().get("items") == []
