"""The local REST service: Section 2.10 in full, plus N-17, N-19, N-22, N-23,
N-31, F-46, F-52, F-57, F-79, and Section 4.1's limits.

The engine is faked here, and deliberately so.  What is under test is the
contract -- who may ask what, in which order the checks run, which status and
which code come back -- and none of that depends on synthesis actually
happening.  The fake still validates through ``jobs.request.validate_request``
and still writes through the real ``Store``, so a code this file asserts is a
code the product produces rather than one the test invented.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from echoact import paths
from echoact.audio import wav
from echoact.config.settings import Settings
from echoact.db.store import Store
from echoact.domain import (
    Budget,
    Capability,
    Gender,
    Job,
    JobState,
    Language,
    RequestPath,
    Result,
    SpeakingStyle,
    TimeRange,
    VoiceSettings,
)
from echoact.errors import Code, EchoActError, http_status, is_retryable
from echoact.jobs.engine import BUSY_RETRY_AFTER_S
from echoact.jobs.request import JobRequest, plan_segments, validate_request
from echoact.models.catalog import MANIFEST, SUPERTONIC_3_ID
from echoact.models.registry import ModelRegistry
from echoact.policy import (
    ALLOWED_HOSTS,
    API_PREFIX,
    AUTH_FAILURES_PER_MIN,
    AUTH_LOCKOUT_S,
    BOUNDED_WAIT_CEILING_S,
    LIST_PAGE_MAX,
    MAX_REQUEST_BODY_BYTES,
    ONEOFF_RESULT_TTL_S,
    RATE_GENERATION_PER_MIN,
    RATE_OTHER_PER_MIN,
    REST_HOST,
)
from echoact.security.credentials import CredentialStore
from echoact.security.ratelimit import RateLimiter, RequestClass
from echoact.service.app import CONTRACT_OPERATIONS, CONTRACT_PATHS, create_app
from echoact.service.deps import ServiceContext, check_host, check_origin, classify
from echoact.service.server import ServiceRunner
from echoact.util import ids

SR = 44_100
PORT = 8765
BASE_URL = f"http://{REST_HOST}:{PORT}"

KO = "에코액트는 문서를 소리내어 읽어 줍니다. 두 번째 문장입니다. 세 번째 문장입니다."
EN = "EchoAct reads a document aloud. This is the second sentence."

VOICE = {
    "model_id": SUPERTONIC_3_ID,
    "voice_id": "F1",
    "gender": "female",
    "language": "auto",
    "style": "natural",
    "tempo": 1.0,
}

BUDGET = Budget(cpu_percent=20, memory_bytes=2 << 30, intra_op_threads=2)


# ======================================================================
# Harness
# ======================================================================


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ECHOACT_MODEL_DIR", str(tmp_path / "models"))
    paths.data_dir.cache_clear()
    paths.ensure_tree()
    yield
    paths.data_dir.cache_clear()


class FakeEngine:
    """The one generation slot, without the synthesis.

    It takes the slot, claims the duplicate-prevention key, and plans the
    segments exactly as ``JobEngine`` does -- through the same validation and
    the same store -- so the service sees the same objects and the same
    errors.  What it does not do is run a worker: the tests drive a job to
    completion themselves, which is both faster and the only way to assert on
    a half-finished job at a chosen moment.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.manifest = MANIFEST
        self.current_job: Job | None = None
        self.model_resident = True
        self.refuse_with: EchoActError | None = None
        self.cancelled: list[str] = []
        self.waits: list[tuple[str, float]] = []
        self.cancel_blocks_for_s = 0.0
        self.finish_on_wait = False

    # -- what the service reads ----------------------------------------

    @property
    def busy(self) -> bool:
        return self.current_job is not None

    def current(self) -> Job | None:
        return self.current_job

    def would_need_load(self, settings: VoiceSettings | None = None) -> bool:
        return not self.model_resident

    # -- what the service calls ----------------------------------------

    def submit(self, request: JobRequest) -> tuple[Job, bool]:
        validate_request(request, self.manifest)
        if self.refuse_with is not None:
            raise self.refuse_with
        if self.current_job is not None:
            raise EchoActError(Code.BUSY, retry_after_s=BUSY_RETRY_AFTER_S)
        job = Job(
            job_id=ids.job_id(),
            kind=request.kind,
            request_path=request.request_path,
            owner_client_id=request.owner_client_id,
            state=JobState.ACCEPTED,
            source_text=request.text,
            settings=request.settings,
            budget=BUDGET,
            retention=request.retention,
            created_at=ids.now(),
            client_label=request.client_label,
            idempotency_key=request.idempotency_key,
        )
        stored, created = self.store.claim_job(
            job,
            client_id=request.owner_client_id,
            key=request.idempotency_key,
            request_digest=request.digest(),
        )
        if not created:
            return stored, False
        stored.segments = list(
            self.store.insert_segments(stored.job_id, plan_segments(request.text, request.settings))
        )
        stored.total_segments = len(stored.segments)
        self.store.set_job_budget(stored.job_id, BUDGET)
        self.current_job = stored
        return stored, True

    def wait(self, job_id: str, timeout_s: float) -> Job:
        self.waits.append((job_id, timeout_s))
        if self.finish_on_wait:
            complete_job(self, job_id)
        return self.store.get_job(job_id)

    def cancel(self, job_id: str) -> Job:
        self.cancelled.append(job_id)
        if self.cancel_blocks_for_s:
            threading.Event().wait(self.cancel_blocks_for_s)
        job = self.store.get_job(job_id, include_segments=False)
        if not job.state.is_terminal:
            self.store.update_job_state(job_id, JobState.CANCELING, force=True)
            self.store.update_job_state(job_id, JobState.CANCELED)
        self.release()
        return self.store.get_job(job_id, include_segments=False)

    # -- test control ---------------------------------------------------

    def release(self) -> None:
        self.current_job = None


class FakeApplication:
    """Just enough of ``Application`` for the service to be composed against.

    The store, the registry, the credential store and the rate limiter are all
    the real ones -- only the engine is faked -- so ownership, capability,
    rate, and persistence behaviour under test is the product's own.
    """

    def __init__(self) -> None:
        self.store = Store(paths.data_dir() / "db.sqlite3", audio_root=paths.audio_dir())
        # No package-cache fallback: the registry would otherwise find the
        # ``supertonic`` package's own weights on a developer machine and
        # report the model ready, which would make "an undownloaded model is
        # distinguished from a fault" pass or fail by whose laptop ran it.
        self.registry = ModelRegistry(
            MANIFEST, root=paths.model_cache_dir(), package_cache_dirs={}
        )
        self.credentials = CredentialStore.load()
        self.limiter = RateLimiter()
        self.settings = Settings(voice=_voice_settings())
        self.engine = FakeEngine(self.store)

    def close(self) -> None:
        self.store.close()


@dataclass
class Api:
    client: TestClient
    context: ServiceContext
    application: FakeApplication
    tokens: dict[str, str]
    clients: dict[str, str]

    @property
    def engine(self) -> FakeEngine:
        return self.application.engine

    @property
    def store(self) -> Store:
        return self.application.store

    def request(self, method: str, path: str, *, actor: str | None = "client", **kw: Any):
        headers = dict(kw.pop("headers", {}) or {})
        if actor is not None:
            headers.setdefault("Authorization", f"Bearer {self.tokens[actor]}")
        return self.client.request(method, path, headers=headers, **kw)

    def get(self, path: str, **kw: Any):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any):
        return self.request("POST", path, **kw)


@pytest.fixture()
def api() -> Any:
    application = FakeApplication()
    tokens: dict[str, str] = {}
    clients: dict[str, str] = {}
    grants = {
        "owner": {Capability.OWNER},
        "client": {Capability.GENERATE, Capability.READ_RESULTS, Capability.READ_HISTORY},
        "generator": {Capability.GENERATE},
        "reader": {Capability.READ_RESULTS},
        "other": {Capability.GENERATE, Capability.READ_RESULTS, Capability.READ_HISTORY},
    }
    for name, capabilities in grants.items():
        issued = application.credentials.issue(name=name, capabilities=capabilities)
        tokens[name] = issued.token
        clients[name] = issued.client_id

    context = ServiceContext(application, port=PORT)
    app = create_app(context)
    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        yield Api(client, context, application, tokens, clients)
    context.close()
    application.close()


# ---------------------------------------------------------------- helpers ---


def _voice_settings() -> VoiceSettings:
    return VoiceSettings(
        model_id=SUPERTONIC_3_ID,
        language=Language.AUTO,
        gender=Gender.FEMALE,
        voice_id="F1",
        style=SpeakingStyle.NATURAL,
        tempo=1.0,
    )


def create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "speech",
        "idempotency_key": "key-1",
        "text": EN,
        "voice": dict(VOICE),
    }
    body.update(overrides)
    return body


def multipart_form(fields: dict[str, str], *, boundary: str = "echoact-boundary") -> tuple[bytes, dict[str, str]]:
    """A multipart body encoded by hand, because httpx will not send one
    without a file part and the request part is a plain field."""
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + value.encode("utf-8")
        + b"\r\n"
        for name, value in fields.items()
    )
    body += f"--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def accepted_job(api: Api, *, actor: str = "client", **overrides: Any) -> str:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(**overrides), actor=actor)
    assert response.status_code in (200, 202), response.text
    return response.json()["job_id"]


def complete_job(engine: FakeEngine, job_id: str, *, retained: bool = False) -> Job:
    """Drive a job to Complete the way the engine would, with real WAV files.

    Real files rather than empty ones: the audio routes probe and stream them,
    and a test that asserted on a zero-byte placeholder would prove nothing
    about F-82's format or N-21's streaming.
    """
    store = engine.store
    job = store.get_job(job_id)
    if job.state is JobState.ACCEPTED:
        store.update_job_state(job_id, JobState.PREPARING_MODEL)
    if job.state is not JobState.GENERATING:
        store.update_job_state(job_id, JobState.GENERATING)

    scratch = paths.temp_dir() / job_id
    scratch.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    start_frame = 0
    for segment in job.segments:
        if not segment.is_spoken:
            span = TimeRange(
                wav.ms_for_frames(start_frame, SR), wav.ms_for_frames(start_frame, SR)
            )
            store.mark_segment_ready(job_id, segment.index, time=span, audio_path=None)
            continue
        frames = max(1, int(len(segment.spoken_text) / 6.0 * SR))
        out = scratch / f"{segment.index:05d}.wav"
        t = np.arange(frames, dtype=np.float32) / SR
        wav.write_segment(out, (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SR)
        span = TimeRange(
            wav.ms_for_frames(start_frame, SR), wav.ms_for_frames(start_frame + frames, SR)
        )
        store.mark_segment_ready(
            job_id, segment.index, time=span, audio_path=str(out), frame_count=frames
        )
        written.append(str(out))
        start_frame += frames

    root = paths.audio_dir() if retained else paths.temp_dir()
    result_path = root / job_id / "result.wav"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    report = wav.concatenate(
        written, gaps_ms=[0] * len(written), out_path=result_path, sample_rate=SR
    )
    now = ids.now()
    store.attach_result(
        Result(
            result_id=ids.result_id(),
            job_id=job_id,
            sample_rate=SR,
            channels=1,
            sample_width_bits=16,
            frame_count=report.frame_count,
            byte_size=report.byte_size,
            digest=wav.digest(result_path),
            created_at=now,
            expires_at=None if retained else now + ONEOFF_RESULT_TTL_S,
            relative_path=str(result_path),
        )
    )
    store.update_job_state(job_id, JobState.COMPLETE)
    engine.release()
    return store.get_job(job_id)


def error_of(response) -> dict[str, Any]:
    body = response.json()
    assert {"code", "message", "retryable", "request_id"} <= set(body), body
    return body


# ======================================================================
# F-57 -- the contract and its machine-readable form
# ======================================================================


def test_the_specification_describes_the_twelve_contract_operations_and_no_others(api: Api) -> None:
    spec = api.get("/openapi.json").json()
    found = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }
    assert found == set(CONTRACT_OPERATIONS)
    assert len(CONTRACT_OPERATIONS) == 12
    assert len(CONTRACT_PATHS) == 11, "create and history share one path"


def test_the_specification_defines_every_schema_it_references(api: Api) -> None:
    """F-57: a document that references a schema it does not define is not
    machine-readable, and POST /jobs' two alternative bodies are exactly where
    that would happen."""
    spec = api.get("/openapi.json").json()
    defined = set(spec["components"]["schemas"])
    referenced = set(_refs(spec))
    assert "CreateJobRequest" in defined
    assert referenced <= defined, sorted(referenced - defined)


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value.rsplit("/", 1)[-1])
            else:
                found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


def test_the_specification_carries_request_and_response_examples(api: Api) -> None:
    """F-57 asks for examples alongside the contract, and the refusals a
    client must handle are the ones worth showing it in advance."""
    spec = api.get("/openapi.json").json()
    create = spec["components"]["schemas"]["CreateJobRequest"]
    assert create["examples"][0]["idempotency_key"]
    assert create["examples"][0]["voice"]["voice_id"]

    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            failure = operation["responses"]["default"]["content"]["application/json"]
            assert failure["schema"]["$ref"].endswith("ErrorBody"), (method, path)
            example = failure["examples"][Code.BUSY.value]["value"]
            assert example["code"] == Code.BUSY.value
            assert example["retryable"] is True


def test_the_specification_documents_only_responses_the_service_produces(api: Api) -> None:
    """F-57's document has to be usable as the contract, so it may not
    advertise a status this service never answers nor a shape it never
    returns.  FastAPI's automatic 422 is both."""
    spec = api.get("/openapi.json").json()
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            assert "422" not in operation["responses"], (method, path)
    create = spec["paths"][f"{API_PREFIX}/jobs"]["post"]["responses"]
    assert set(create) == {"200", "202", "default"}
    cancel = spec["paths"][f"{API_PREFIX}/jobs/{{job_id}}/cancel"]["post"]["responses"]
    assert set(cancel) == {"200", "202", "default"}


def test_the_specification_names_the_interface_version(api: Api) -> None:
    """F-57 and N-24: the contract is versioned distinctly from the build."""
    spec = api.get("/openapi.json").json()
    assert spec["info"]["x-api-version"] == "v1"
    assert spec["info"]["version"]


def test_every_error_carries_a_code_a_message_a_retry_flag_and_a_request_id(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/jobs/job_missing")
    assert response.status_code == 404
    body = error_of(response)
    assert body["code"] == Code.NOT_FOUND.value
    assert body["retryable"] is False
    assert body["request_id"].startswith("req_")
    assert response.headers["X-Request-Id"] == body["request_id"]


def test_a_retryable_refusal_carries_a_hint_and_a_permanent_one_does_not(api: Api) -> None:
    """Rule 8, on the wire: busy is retryable and says when, a missing job is
    not and says nothing."""
    accepted_job(api)
    busy = api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="key-2", text=KO))
    assert busy.status_code == 409
    assert error_of(busy)["code"] == Code.BUSY.value
    assert error_of(busy)["retry_after_s"] > 0
    assert int(busy.headers["Retry-After"]) >= 1

    permanent = api.get(f"{API_PREFIX}/jobs/job_missing")
    assert "retry_after_s" not in permanent.json()
    assert "Retry-After" not in permanent.headers


def test_a_successful_response_also_carries_its_request_id(api: Api) -> None:
    """N-25 traces a problem by request identifier, and a streamed WAV has no
    JSON body to put one in -- so it goes in a header on every answer."""
    ok = api.get(f"{API_PREFIX}/status")
    assert ok.headers["X-Request-Id"].startswith("req_")

    job_id = accepted_job(api, retain=True)
    complete_job(api.engine, job_id, retained=True)
    streamed = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert streamed.headers["X-Request-Id"].startswith("req_")


def test_the_service_never_reaches_the_player(api: Api) -> None:
    """F-51: an external request generates only and never plays on the host
    speakers.  The application this service is composed against carries no
    player at all, so any attempt to reach one would fail the request."""
    assert not hasattr(api.application, "player")
    job_id = accepted_job(api, retain=True)
    complete_job(api.engine, job_id, retained=True)
    assert api.get(f"{API_PREFIX}/jobs/{job_id}/audio").status_code == 200
    assert api.get(f"{API_PREFIX}/jobs/{job_id}").json()["state"] == JobState.COMPLETE.value


def test_an_error_never_carries_an_internal_path(api: Api) -> None:
    responses = [
        api.get(f"{API_PREFIX}/jobs/job_missing"),
        api.post(f"{API_PREFIX}/jobs", json=create_body(voice={**VOICE, "model_id": "nope"})),
        api.get(f"{API_PREFIX}/jobs", actor="generator"),
    ]
    for response in responses:
        rendered = response.text
        assert str(paths.data_dir()) not in rendered
        assert "C:\\" not in rendered and "/home/" not in rendered


# ======================================================================
# N-17 -- Host and Origin, before authentication
# ======================================================================


def test_an_unexpected_host_is_refused_before_the_credential_is_looked_at(api: Api) -> None:
    """N-17 fixes the order, so the proof is that a bad Host with *no*
    credential answers the Host's status and not 401."""
    response = api.get(f"{API_PREFIX}/status", actor=None, headers={"Host": "example.com"})
    assert response.status_code == http_status(Code.HOST_NOT_ALLOWED) == 400
    assert error_of(response)["code"] == Code.HOST_NOT_ALLOWED.value


def test_a_bad_host_does_not_count_against_the_authentication_failure_limit(api: Api) -> None:
    for _ in range(AUTH_FAILURES_PER_MIN + 2):
        api.get(f"{API_PREFIX}/status", actor=None, headers={"Host": "evil.test"})
    assert api.context.auth_failures.check("testclient").failures == 0
    assert api.get(f"{API_PREFIX}/status").status_code == 200


def test_an_unexpected_origin_is_refused_before_authentication(api: Api) -> None:
    response = api.get(
        f"{API_PREFIX}/status", actor=None, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.ORIGIN_NOT_ALLOWED.value


def test_a_loopback_origin_on_another_port_is_still_refused(api: Api) -> None:
    """A page served by another program on this machine is not this service's
    origin, and CORS being disallowed by default means it is refused rather
    than answered without the header."""
    response = api.get(f"{API_PREFIX}/status", headers={"Origin": f"http://127.0.0.1:{PORT + 1}"})
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.ORIGIN_NOT_ALLOWED.value


def test_a_host_claiming_another_port_is_refused(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/status", headers={"Host": f"127.0.0.1:{PORT + 1}"})
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.HOST_NOT_ALLOWED.value


def test_no_response_ever_carries_a_cross_origin_header(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/status")
    assert response.status_code == 200
    assert not [h for h in response.headers if h.lower().startswith("access-control-")]


@pytest.mark.parametrize("host", sorted(ALLOWED_HOSTS))
def test_every_allowed_host_is_accepted_with_and_without_the_port(host: str) -> None:
    check_host(host, PORT)
    bare = host.strip("[]")
    check_host(f"[{bare}]:{PORT}" if ":" in bare else f"{bare}:{PORT}", PORT)


def test_a_missing_host_header_is_refused_rather_than_tolerated() -> None:
    with pytest.raises(EchoActError) as caught:
        check_host(None, PORT)
    assert caught.value.code is Code.HOST_NOT_ALLOWED


def test_an_absent_origin_is_the_ordinary_case_and_is_allowed() -> None:
    check_origin(None, PORT)


def test_only_creating_a_job_is_counted_as_a_generation_request() -> None:
    """4.1 counts generation apart from everything else; F-88's estimate makes
    no job, so charging it to the generation allowance would price the
    pre-flight like the work it exists to avoid."""
    assert classify("POST", f"{API_PREFIX}/jobs") is RequestClass.GENERATION
    assert classify("GET", f"{API_PREFIX}/jobs") is RequestClass.OTHER
    assert classify("POST", f"{API_PREFIX}/estimate") is RequestClass.OTHER
    assert classify("POST", f"{API_PREFIX}/jobs/job_1/cancel") is RequestClass.OTHER


# ======================================================================
# N-31 -- authentication, always, fail closed
# ======================================================================


@pytest.mark.parametrize("method,path", CONTRACT_OPERATIONS)
def test_no_contract_operation_answers_without_a_credential(
    api: Api, method: str, path: str
) -> None:
    concrete = path.replace("{job_id}", "job_x").replace("{segment_id}", "seg_x")
    response = api.request(method, concrete, actor=None)
    assert response.status_code == 401
    assert error_of(response)["code"] == Code.UNAUTHENTICATED.value


def test_the_specification_itself_requires_a_credential(api: Api) -> None:
    """N-31: no request succeeds without one, and the document is a request."""
    assert api.get("/openapi.json", actor=None).status_code == 401
    assert api.get("/openapi.json").status_code == 200


def test_an_unknown_path_answers_unauthenticated_rather_than_not_found(api: Api) -> None:
    """Failing closed means an unauthenticated caller does not get to map the
    surface by watching which paths 404."""
    assert api.get(f"{API_PREFIX}/documents", actor=None).status_code == 401
    assert api.get(f"{API_PREFIX}/documents").status_code == 404


def test_a_token_in_the_query_string_is_not_a_credential(api: Api) -> None:
    """N-17 keeps tokens out of URLs, which only holds if the service refuses
    to read one from there."""
    response = api.get(f"{API_PREFIX}/status?token={api.tokens['client']}", actor=None)
    assert response.status_code == 401


def test_a_garbled_token_is_refused_without_saying_why(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/status", headers={"Authorization": "Bearer eak_no_such"})
    assert response.status_code == 401
    assert error_of(response)["code"] == Code.UNAUTHENTICATED.value


def test_a_revoked_credential_stops_working_immediately(api: Api) -> None:
    assert api.get(f"{API_PREFIX}/status").status_code == 200
    reference = next(c.ref for c in api.application.credentials.list() if c.name == "client")
    api.application.credentials.revoke(reference)
    response = api.get(f"{API_PREFIX}/status")
    assert response.status_code == 401
    assert error_of(response)["code"] == Code.CREDENTIAL_REVOKED.value


def test_an_expired_credential_is_refused(api: Api) -> None:
    credential = next(c for c in api.application.credentials.list() if c.name == "client")
    credential.expires_at = ids.now() - 1.0
    response = api.get(f"{API_PREFIX}/status")
    assert response.status_code == 401
    assert error_of(response)["code"] == Code.CREDENTIAL_EXPIRED.value


def test_ten_authentication_failures_lock_that_origin_out_for_a_minute(api: Api) -> None:
    for _ in range(AUTH_FAILURES_PER_MIN):
        assert api.get(f"{API_PREFIX}/status", actor=None).status_code == 401
    locked = api.get(f"{API_PREFIX}/status")
    assert locked.status_code == 429
    body = error_of(locked)
    assert body["code"] == Code.AUTH_LOCKED_OUT.value
    assert body["retry_after_s"] == pytest.approx(AUTH_LOCKOUT_S, abs=1.0)


def test_a_lockout_stops_neither_the_service_nor_another_origin(api: Api) -> None:
    """4.1: the local service as a whole and the GUI are not terminated."""
    for _ in range(AUTH_FAILURES_PER_MIN):
        api.get(f"{API_PREFIX}/status", actor=None)
    assert api.get(f"{API_PREFIX}/status").status_code == 429

    elsewhere = TestClient(
        api.client.app,
        base_url=BASE_URL,
        raise_server_exceptions=False,
        client=("127.0.0.2", 4444),
    )
    served = elsewhere.get(
        f"{API_PREFIX}/status", headers={"Authorization": f"Bearer {api.tokens['client']}"}
    )
    assert served.status_code == 200
    # The GUI's own parts are untouched: nothing was shut down to enforce it.
    assert api.store.list_jobs().total == 0


def test_a_valid_credential_clears_the_failures_that_came_before_it(api: Api) -> None:
    for _ in range(AUTH_FAILURES_PER_MIN - 1):
        api.get(f"{API_PREFIX}/status", actor=None)
    assert api.get(f"{API_PREFIX}/status").status_code == 200
    assert api.context.auth_failures.check("testclient").failures == 0


# ======================================================================
# F-50, F-61, N-19 -- capability and ownership
# ======================================================================


def test_a_credential_without_the_capability_is_refused_with_403(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/jobs", actor="generator")
    assert response.status_code == 403
    assert error_of(response)["code"] == Code.FORBIDDEN.value


def test_reading_a_result_needs_its_own_capability(api: Api) -> None:
    job_id = accepted_job(api)
    complete_job(api.engine, job_id)
    assert api.get(f"{API_PREFIX}/jobs/{job_id}/result", actor="client").status_code == 200
    assert api.get(f"{API_PREFIX}/jobs/{job_id}/result", actor="generator").status_code == 403


def test_the_source_text_snapshot_is_separately_authorised(api: Api) -> None:
    job_id = accepted_job(api, retain=True)
    assert api.get(f"{API_PREFIX}/jobs/{job_id}/text", actor="client").status_code == 200
    denied = api.get(f"{API_PREFIX}/jobs/{job_id}/text", actor="generator")
    assert denied.status_code == 403
    assert error_of(denied)["code"] == Code.FORBIDDEN.value


def test_another_clients_job_is_not_found_rather_than_forbidden(api: Api) -> None:
    """N-19: knowing a job id grants nothing, and 403 would confirm the id
    exists.  404 makes the answer identical either way."""
    job_id = accepted_job(api)
    mine = api.get(f"{API_PREFIX}/jobs/{job_id}", actor="client")
    theirs = api.get(f"{API_PREFIX}/jobs/{job_id}", actor="other")
    imaginary = api.get(f"{API_PREFIX}/jobs/job_deadbeef", actor="other")

    assert mine.status_code == 200
    assert theirs.status_code == 404
    assert imaginary.status_code == 404
    assert error_of(theirs)["code"] == error_of(imaginary)["code"] == Code.NOT_FOUND.value


@pytest.mark.parametrize(
    "suffix", ["", "/text", "/segments", "/audio", "/result"]
)
def test_every_job_route_checks_ownership(api: Api, suffix: str) -> None:
    job_id = accepted_job(api, retain=True)
    complete_job(api.engine, job_id, retained=True)
    assert api.get(f"{API_PREFIX}/jobs/{job_id}{suffix}", actor="other").status_code == 404


def test_cancelling_another_clients_job_is_not_found(api: Api) -> None:
    job_id = accepted_job(api)
    response = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel", actor="other")
    assert response.status_code == 404
    assert api.engine.cancelled == []


def test_the_owner_credential_may_review_every_clients_job(api: Api) -> None:
    """F-50: the GUI owner reviews and cancels all jobs."""
    job_id = accepted_job(api, retain=True)
    assert api.get(f"{API_PREFIX}/jobs/{job_id}", actor="owner").status_code == 200
    page = api.get(f"{API_PREFIX}/jobs", actor="owner").json()
    assert [item["job_id"] for item in page["items"]] == [job_id]


def test_a_segment_of_another_clients_job_is_not_found(api: Api) -> None:
    job_id = accepted_job(api)
    complete_job(api.engine, job_id)
    segment_id = api.get(f"{API_PREFIX}/jobs/{job_id}/segments").json()["segments"][0]["segment_id"]
    denied = api.get(f"{API_PREFIX}/jobs/{job_id}/segments/{segment_id}/audio", actor="other")
    assert denied.status_code == 404


# ======================================================================
# 4.1 / N-23 -- body size and request rate
# ======================================================================


def test_a_declared_body_over_the_cap_is_refused_before_it_is_read(api: Api) -> None:
    oversized = b"{" + b" " * MAX_REQUEST_BODY_BYTES
    response = api.post(
        f"{API_PREFIX}/jobs", content=oversized, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert error_of(response)["code"] == Code.PAYLOAD_TOO_LARGE.value
    assert error_of(response)["detail"]["limit_bytes"] == MAX_REQUEST_BODY_BYTES


def test_an_undeclared_body_is_counted_as_it_arrives(api: Api) -> None:
    """A chunked body declares no length, so the cap has to be enforced on the
    stream as well; otherwise "capped at 2,000,000 bytes" would hold only for
    clients that volunteer their size."""
    half = MAX_REQUEST_BODY_BYTES // 2 + 1

    def chunks():
        yield b"x" * half
        yield b"x" * half

    response = api.post(
        f"{API_PREFIX}/jobs", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_an_upload_is_measured_with_its_metadata_included(api: Api) -> None:
    """4.1 caps the request body "including upload metadata", so the multipart
    envelope counts and a file just under the file limit can still be too
    large for a request."""
    payload = b"a" * (MAX_REQUEST_BODY_BYTES - 100)
    response = api.post(
        f"{API_PREFIX}/jobs",
        data={"request": json.dumps(create_body(text=None))},
        files={"file": ("big.txt", payload, "text/plain")},
    )
    assert response.status_code == 413


def test_the_generation_allowance_is_separate_from_the_other_allowance(api: Api) -> None:
    client_id = api.clients["client"]
    for _ in range(RATE_GENERATION_PER_MIN):
        api.application.limiter.check(client_id, RequestClass.GENERATION)

    refused = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert refused.status_code == 429
    body = error_of(refused)
    assert body["code"] == Code.RATE_LIMITED.value
    assert body["retry_after_s"] > 0
    assert "Retry-After" in refused.headers
    # The other allowance is untouched: 4.1 states the two as separate limits.
    assert api.get(f"{API_PREFIX}/status").status_code == 200


def test_the_other_allowance_limits_queries(api: Api) -> None:
    client_id = api.clients["client"]
    for _ in range(RATE_OTHER_PER_MIN):
        api.application.limiter.check(client_id, RequestClass.OTHER)
    refused = api.get(f"{API_PREFIX}/status")
    assert refused.status_code == 429
    assert error_of(refused)["code"] == Code.RATE_LIMITED.value


def test_a_rate_limit_is_per_client_and_not_per_service(api: Api) -> None:
    for _ in range(RATE_OTHER_PER_MIN):
        api.application.limiter.check(api.clients["client"], RequestClass.OTHER)
    assert api.get(f"{API_PREFIX}/status").status_code == 429
    assert api.get(f"{API_PREFIX}/status", actor="reader").status_code == 200


# ======================================================================
# F-49 / F-54 -- the request's own shape
# ======================================================================


def test_a_generation_request_without_a_duplicate_prevention_key_is_refused(api: Api) -> None:
    body = create_body()
    del body["idempotency_key"]
    response = api.post(f"{API_PREFIX}/jobs", json=body)
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.IDEMPOTENCY_KEY_MISSING.value


def test_an_empty_duplicate_prevention_key_is_refused(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key=""))
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.IDEMPOTENCY_KEY_MISSING.value


def test_the_same_key_with_the_same_content_returns_the_same_job(api: Api) -> None:
    first = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert first.status_code == 202
    api.engine.release()
    second = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["duplicate"] is True


def test_the_same_key_with_different_content_is_a_conflict(api: Api) -> None:
    api.post(f"{API_PREFIX}/jobs", json=create_body())
    api.engine.release()
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(text=KO))
    assert response.status_code == 409
    assert error_of(response)["code"] == Code.IDEMPOTENCY_KEY_CONFLICT.value


def test_a_key_is_scoped_to_the_client_that_minted_it(api: Api) -> None:
    """4.1 scopes re-request identifiers per client, so two clients using the
    same string are two requests and not a conflict."""
    mine = api.post(f"{API_PREFIX}/jobs", json=create_body())
    api.engine.release()
    theirs = api.post(f"{API_PREFIX}/jobs", json=create_body(), actor="other")
    assert theirs.status_code == 202
    assert theirs.json()["job_id"] != mine.json()["job_id"]


def test_a_request_without_a_job_kind_is_refused(api: Api) -> None:
    body = create_body()
    del body["kind"]
    response = api.post(f"{API_PREFIX}/jobs", json=body)
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.JOB_KIND_MISSING.value


def test_an_unrecognised_job_kind_is_refused_rather_than_defaulted(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(kind="podcast"))
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.JOB_KIND_MISSING.value
    assert api.store.list_jobs().total == 0


def test_a_body_that_is_not_an_object_is_refused_without_creating_a_job(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/jobs", content=b"[]", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert api.store.list_jobs().total == 0


def test_a_misspelled_option_is_refused_rather_than_silently_defaulted(api: Api) -> None:
    """F-54: unknown options are not arbitrarily substituted.

    Dropping ``retainn`` substitutes ``retain: false`` just as surely as
    replacing it would: the caller asked for a retained job and would get a
    202 for a one-off one whose text and audio 4.1 removes an hour after it
    ends, with nothing in the answer to tell the two apart.
    """
    body = create_body()
    body["retainn"] = True
    body["wait"] = 10

    response = api.post(f"{API_PREFIX}/jobs", json=body)
    assert response.status_code == 400, response.text
    refusal = error_of(response)
    assert set(refusal["detail"]["unknown_fields"]) == {"retainn", "wait"}
    assert api.store.list_jobs().total == 0
    assert api.engine.waits == []


def test_a_misspelled_voice_option_is_refused_as_an_invalid_voice_setting(api: Api) -> None:
    """A caller that asked for 1.5x and was given 1.0x has been handed audio
    it did not ask for, so Section 2.10's 422 for voice settings applies."""
    response = api.post(
        f"{API_PREFIX}/jobs", json=create_body(voice={**VOICE, "speed": 1.5})
    )
    assert response.status_code == 422, response.text
    assert error_of(response)["detail"]["unknown_fields"] == ["speed"]
    assert api.store.list_jobs().total == 0


def test_an_estimate_refuses_an_option_it_does_not_define(api: Api) -> None:
    """N-24: the pre-flight answers about the request that would be sent, so
    it cannot accept a body the create route refuses."""
    response = api.post(f"{API_PREFIX}/estimate", json={"text": EN, "voice": VOICE, "wait": 5})
    assert response.status_code == 400, response.text
    assert error_of(response)["detail"]["unknown_fields"] == ["wait"]


def test_an_unsupported_content_type_is_415(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/jobs", content=b"hello", headers={"Content-Type": "application/xml"}
    )
    assert response.status_code == 415
    assert error_of(response)["code"] == Code.FILE_UNSUPPORTED.value


# ======================================================================
# F-04 to F-08, F-03 -- validation
# ======================================================================


@pytest.mark.parametrize(
    "override,code",
    [
        ({"model_id": "not-a-model"}, Code.MODEL_UNKNOWN),
        ({"voice_id": "Z9"}, Code.VOICE_UNKNOWN),
        ({"gender": "male"}, Code.VOICE_GENDER_MISMATCH),
        ({"tempo": 3.0}, Code.TEMPO_OUT_OF_RANGE),
        ({"style": "operatic"}, Code.STYLE_UNKNOWN),
        ({"language": "fr"}, Code.LANGUAGE_UNKNOWN),
    ],
)
def test_invalid_voice_settings_are_422_with_the_code_that_names_them(
    api: Api, override: dict[str, Any], code: Code
) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(voice={**VOICE, **override}))
    assert response.status_code == 422
    assert error_of(response)["code"] == code.value


def test_empty_text_is_refused(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(text="   "))
    assert response.status_code == 422
    assert error_of(response)["code"] == Code.INPUT_EMPTY.value


def test_text_over_the_input_limit_is_413(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(text="가" * 50_001))
    assert response.status_code == 413
    assert error_of(response)["code"] == Code.INPUT_TOO_LONG.value


def test_neither_inline_text_nor_an_upload_is_refused(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(text=None))
    assert response.status_code == 422
    assert error_of(response)["code"] == Code.INPUT_EMPTY.value


# ======================================================================
# F-54 -- uploads
# ======================================================================


def test_an_upload_becomes_the_jobs_source_text(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/jobs",
        data={"request": json.dumps(create_body(text=None, retain=True))},
        files={"file": ("note.md", EN.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert api.store.get_job_text(job_id) == EN


def test_inline_text_and_an_upload_together_are_refused(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/jobs",
        data={"request": json.dumps(create_body())},
        files={"file": ("note.txt", b"other text", "text/plain")},
    )
    assert response.status_code == 400
    assert error_of(response)["code"] == Code.INPUT_AMBIGUOUS.value


def test_an_upload_that_is_not_text_is_refused_by_the_shared_loader(api: Api) -> None:
    """F-37: an automated caller reaches the identical checks the file dialog
    does, so a PNG is refused here for the reason it is refused there."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
    response = api.post(
        f"{API_PREFIX}/jobs",
        data={"request": json.dumps(create_body(text=None))},
        files={"file": ("cover.png", png, "image/png")},
    )
    assert response.status_code == 415
    assert error_of(response)["code"] in {
        Code.FILE_UNSUPPORTED.value,
        Code.FILE_NOT_TEXT.value,
    }


def test_a_multipart_request_without_its_description_is_refused(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/jobs", files={"file": ("note.txt", EN.encode("utf-8"), "text/plain")}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("multipart/form-data; boundary=abc", b"garbage"),
        ("multipart/form-data", b"--abc\r\ngarbage\r\n--abc--\r\n"),
        ("multipart/form-data; boundary=abc", b"--abc\r\nContent-Disposition: form-data\r\n\r\nx"),
    ],
)
def test_a_multipart_body_that_will_not_parse_is_400_and_not_a_server_fault(
    api: Api, content_type: str, body: bytes
) -> None:
    """Section 2.10 gives a malformed request 400 with a detailed code.

    The form is read by hand here, which steps around the 400 FastAPI wraps
    its own form handling in, so the parser's refusal has to be translated or
    it reaches the unexpected-exception handler as a 500 -- a status Section
    2.10 does not list for a malformed body, in an envelope whose
    ``retryable: false`` and ``INTERNAL`` tell a client nothing to act on.
    """
    response = api.post(
        f"{API_PREFIX}/jobs", content=body, headers={"Content-Type": content_type}
    )
    assert response.status_code == 400, response.text
    refusal = error_of(response)
    assert refusal["code"] != Code.INTERNAL.value
    assert refusal["retryable"] is False
    assert api.store.list_jobs().total == 0


def test_a_part_within_the_body_cap_is_judged_by_this_services_own_limits(api: Api) -> None:
    """4.1's 2,000,000-byte request cap is the only size limit on a body.

    Starlette's parser defaults to refusing any single part over 1 MiB, which
    bites first and is not a limit this contract states anywhere; a part of a
    legal size must reach the checks that do have a home in ``policy`` and be
    answered by their code, not by a 500 from the parser.
    """
    body, headers = multipart_form({"request": json.dumps(create_body(text="a" * 1_200_000))})
    assert len(body) < MAX_REQUEST_BODY_BYTES
    response = api.post(f"{API_PREFIX}/jobs", content=body, headers=headers)
    assert response.status_code == 413, response.text
    assert error_of(response)["code"] == Code.INPUT_TOO_LONG.value


def test_a_legal_multipart_body_is_read_the_same_way_when_it_is_encoded_by_hand(api: Api) -> None:
    """The guard above only means something if the ordinary body still works."""
    body, headers = multipart_form({"request": json.dumps(create_body())})
    response = api.post(f"{API_PREFIX}/jobs", content=body, headers=headers)
    assert response.status_code == 202, response.text


# ======================================================================
# F-47 -- the single slot
# ======================================================================


def test_a_second_concurrent_generation_is_refused_with_a_retry_after_hint(api: Api) -> None:
    first = api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="a"))
    assert first.status_code == 202
    second = api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="b", text=KO))
    assert second.status_code == 409
    body = error_of(second)
    assert body["code"] == Code.BUSY.value
    assert body["retryable"] is True
    assert body["retry_after_s"] == pytest.approx(BUSY_RETRY_AFTER_S)
    assert int(second.headers["Retry-After"]) >= 1


def test_a_busy_refusal_does_not_consume_the_duplicate_prevention_key(api: Api) -> None:
    """F-49's record identifies a job that exists, and a busy response creates
    none, so the key must still be usable afterwards."""
    api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="a"))
    assert api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="b")).status_code == 409
    api.engine.release()
    retried = api.post(f"{API_PREFIX}/jobs", json=create_body(idempotency_key="b"))
    assert retried.status_code == 202
    assert retried.json()["duplicate"] is False


def test_queries_still_answer_while_the_slot_is_busy(api: Api) -> None:
    """N-22: query and cancellation respond independently of generation."""
    job_id = accepted_job(api)
    assert api.get(f"{API_PREFIX}/status").json()["generation"]["busy"] is True
    assert api.get(f"{API_PREFIX}/jobs/{job_id}").status_code == 200
    assert api.get(f"{API_PREFIX}/jobs/{job_id}/segments").status_code == 200


def test_a_client_is_told_whether_the_running_job_is_its_own(api: Api) -> None:
    accepted_job(api)
    mine = api.get(f"{API_PREFIX}/status").json()["generation"]
    theirs = api.get(f"{API_PREFIX}/status", actor="other").json()["generation"]
    assert mine["current_job_is_mine"] is True and mine["current_job_id"] is not None
    assert theirs["busy"] is True
    assert theirs["current_job_is_mine"] is False
    assert theirs["current_job_id"] is None


# ======================================================================
# F-88 -- bounded wait and pre-flight estimate
# ======================================================================


def test_a_bounded_wait_answers_200_with_the_terminal_state(api: Api) -> None:
    api.engine.finish_on_wait = True
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(wait_s=5))
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == JobState.COMPLETE.value
    assert body["terminal"] is True
    assert body["result_ready"] is True
    assert api.engine.waits == [(body["job_id"], 5.0)]


def test_a_bound_that_passes_returns_the_job_id_unchanged(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(wait_s=1))
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == JobState.ACCEPTED.value
    assert api.store.get_job(body["job_id"]).state is JobState.ACCEPTED


def test_a_wait_is_never_entered_while_a_model_must_be_prepared(api: Api) -> None:
    """F-88: the service never waits on model preparation or download."""
    api.engine.model_resident = False
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(wait_s=30))
    assert response.status_code == 202
    assert api.engine.waits == []
    assert response.json()["waited_s"] == 0.0


def test_a_wait_longer_than_the_ceiling_is_clamped(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/jobs", json=create_body(wait_s=BOUNDED_WAIT_CEILING_S * 10))
    assert response.status_code == 202
    _, asked = api.engine.waits[0]
    assert asked == api.context.bounded_wait_ceiling_s <= BOUNDED_WAIT_CEILING_S


def test_no_wait_is_entered_when_none_was_asked_for(api: Api) -> None:
    api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert api.engine.waits == []


def test_an_estimate_creates_no_job_and_reports_the_slot(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/estimate", json={"text": KO, "voice": VOICE, "kind": "speech"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["segment_count"] >= 1
    assert body["audio_ms"] > 0 and body["synthesis_ms"] > 0
    assert body["approximate"] is True
    assert body["slot_free"] is True
    # The model is not downloaded in a test tree, and F-53 wants that
    # distinguished from a fault rather than reported as one.
    assert body["model_ready"] is False
    assert api.store.list_jobs().total == 0


def test_an_estimate_reports_every_problem_rather_than_the_first(api: Api) -> None:
    response = api.post(
        f"{API_PREFIX}/estimate",
        json={"text": "  ", "voice": {**VOICE, "model_id": "nope"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    codes = {problem["code"] for problem in body["problems"]}
    assert codes == {Code.INPUT_EMPTY.value, Code.MODEL_UNKNOWN.value}


def test_an_estimate_says_the_slot_is_taken(api: Api) -> None:
    accepted_job(api)
    body = api.post(f"{API_PREFIX}/estimate", json={"text": EN, "voice": VOICE}).json()
    assert body["slot_free"] is False


def test_an_estimate_needs_the_generate_capability(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/estimate", json={"text": EN, "voice": VOICE}, actor="reader")
    assert response.status_code == 403


def test_an_estimate_without_text_is_refused(api: Api) -> None:
    response = api.post(f"{API_PREFIX}/estimate", json={"voice": VOICE})
    assert response.status_code == 422
    assert error_of(response)["code"] == Code.INPUT_EMPTY.value


# ======================================================================
# F-55 -- segments and results
# ======================================================================


def test_only_ready_segments_are_listed_with_the_last_sequence_number(api: Api) -> None:
    job_id = accepted_job(api, text=KO)
    api.store.update_job_state(job_id, JobState.PREPARING_MODEL)
    api.store.update_job_state(job_id, JobState.GENERATING)
    job = api.store.get_job(job_id)
    assert len(job.segments) >= 2
    api.store.mark_segment_ready(job_id, 0, time=TimeRange(0, 1_000), frame_count=SR)

    body = api.get(f"{API_PREFIX}/jobs/{job_id}/segments").json()
    assert body["ready_count"] == 1
    assert body["last_sequence"] == 0
    assert body["total_segments"] == len(job.segments)
    assert body["complete"] is False
    assert [s["index"] for s in body["segments"]] == [0]
    assert body["segments"][0]["source_end"] > body["segments"][0]["source_start"]


def test_a_segment_that_is_not_generated_yet_is_409_with_a_hint(api: Api) -> None:
    job_id = accepted_job(api, text=KO)
    segment = api.store.list_segments(job_id)[-1]
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/segments/{segment.segment_id}/audio")
    assert response.status_code == 409
    body = error_of(response)
    assert body["code"] == Code.SEGMENT_NOT_READY.value
    assert body["retry_after_s"] > 0


def test_an_unknown_segment_id_is_404(api: Api) -> None:
    job_id = accepted_job(api)
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/segments/seg_nope/audio")
    assert response.status_code == 404


def test_a_ready_segment_streams_its_own_wav(api: Api) -> None:
    job_id = accepted_job(api, text=KO)
    complete_job(api.engine, job_id)
    listing = api.get(f"{API_PREFIX}/jobs/{job_id}/segments").json()
    segment = listing["segments"][0]
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/segments/{segment['segment_id']}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"
    assert int(response.headers["content-length"]) == len(response.content)
    assert "no-store" in response.headers["cache-control"]


def test_the_full_audio_is_refused_until_the_job_is_complete(api: Api) -> None:
    job_id = accepted_job(api)
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert response.status_code == 409
    body = error_of(response)
    assert body["code"] == Code.RESULT_NOT_READY.value
    assert body["retry_after_s"] > 0


def test_the_full_audio_streams_the_stored_file_byte_for_byte(api: Api) -> None:
    job_id = accepted_job(api, text=KO, retain=True)
    job = complete_job(api.engine, job_id, retained=True)
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert response.status_code == 200
    stored = Path(job.result.relative_path).read_bytes()
    assert response.content == stored
    assert response.headers["content-type"] == "audio/wav"
    assert f'filename="{job_id}.wav"' in response.headers["content-disposition"]
    info = wav.probe(job.result.relative_path)
    assert info.is_output_format and info.sample_rate == SR


def test_the_result_metadata_carries_what_4_2_records(api: Api) -> None:
    job_id = accepted_job(api, text=KO, retain=True)
    job = complete_job(api.engine, job_id, retained=True)
    body = api.get(f"{API_PREFIX}/jobs/{job_id}/result").json()
    assert body["format"] == wav.FORMAT_NAME
    assert body["media_type"] == "audio/wav"
    assert body["sample_rate"] == SR
    assert body["channels"] == 1 and body["sample_width_bits"] == 16
    assert body["byte_size"] == job.result.byte_size
    assert body["duration_ms"] == job.result.duration_ms
    assert body["integrity"] == {
        "algorithm": "sha256",
        "value": job.result.digest,
        "state": "unverified",
    }
    assert body["expires_at"] is None
    assert body["expired"] is False and body["available"] is True


def test_a_one_off_result_reports_when_it_expires(api: Api) -> None:
    job_id = accepted_job(api)
    complete_job(api.engine, job_id)
    body = api.get(f"{API_PREFIX}/jobs/{job_id}/result").json()
    assert body["expires_at"] == pytest.approx(ids.now() + ONEOFF_RESULT_TTL_S, abs=5.0)


def test_an_expired_result_is_reported_as_expired_rather_than_streamed(api: Api) -> None:
    job_id = accepted_job(api)
    job = complete_job(api.engine, job_id)
    api.store.expire_result(job.result.result_id)

    audio = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert audio.status_code == 410
    assert error_of(audio)["code"] == Code.RESULT_EXPIRED.value

    metadata = api.get(f"{API_PREFIX}/jobs/{job_id}/result").json()
    assert metadata["expired"] is True and metadata["available"] is False


def test_a_result_whose_file_vanished_is_reported_as_missing(api: Api) -> None:
    """5.3: display the reason the result cannot be retrieved."""
    job_id = accepted_job(api)
    job = complete_job(api.engine, job_id)
    Path(job.result.relative_path).unlink()
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert response.status_code == 410
    assert error_of(response)["code"] == Code.RESULT_MISSING.value


def test_a_result_row_pointing_outside_the_managed_tree_is_treated_as_missing(
    api: Api, tmp_path: Path
) -> None:
    """N-18: an arbitrary path from an external source is never read."""
    job_id = accepted_job(api)
    job = complete_job(api.engine, job_id)
    stray = tmp_path / "elsewhere.wav"
    stray.write_bytes(Path(job.result.relative_path).read_bytes())
    with api.store.transaction() as conn:
        conn.execute(
            "UPDATE results SET relative_path = ? WHERE result_id = ?",
            (str(stray), job.result.result_id),
        )
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/audio")
    assert response.status_code == 410
    assert error_of(response)["code"] == Code.RESULT_MISSING.value


def test_the_result_route_refuses_before_there_is_one(api: Api) -> None:
    job_id = accepted_job(api)
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/result")
    assert response.status_code == 409
    assert error_of(response)["code"] == Code.RESULT_NOT_READY.value


def _fail(api: Api, job_id: str) -> None:
    api.store.record_job_error(job_id, Code.WORKER_LOST, "The worker stopped.")
    api.engine.release()


def test_a_failed_jobs_result_is_permanently_gone_rather_than_not_ready_yet(api: Api) -> None:
    """Rule 8 and N-23: a permanent refusal carries no retry-after hint.

    A job that has ended will never produce a result, so answering "not ready
    yet, come back in three seconds" is a hint F-47 says a client must honour
    and which would have it poll for ever.  5.3 wants the reason instead, and
    the reason is the job's own terminal state.
    """
    job_id = accepted_job(api)
    _fail(api, job_id)

    for path in ("audio", "result"):
        response = api.get(f"{API_PREFIX}/jobs/{job_id}/{path}")
        refusal = error_of(response)
        assert response.status_code == 410, path
        assert refusal["retryable"] is False, path
        assert "retry_after_s" not in refusal, path
        assert "Retry-After" not in response.headers, path
        assert refusal["code"] != Code.RESULT_NOT_READY.value, path
        assert refusal["detail"]["state"] == JobState.FAILED.value
        assert refusal["detail"]["error_code"] == Code.WORKER_LOST.value


def test_a_cancelled_jobs_segment_is_permanently_gone_rather_than_not_ready_yet(api: Api) -> None:
    """The same reasoning the segment route already applies to a segment that
    carries no audio: "not ready" invites a retry, and this one will never
    become ready either."""
    job_id = accepted_job(api, text=KO)
    segment = api.store.list_segments(job_id)[-1]
    assert api.post(f"{API_PREFIX}/jobs/{job_id}/cancel").status_code == 202

    response = api.get(f"{API_PREFIX}/jobs/{job_id}/segments/{segment.segment_id}/audio")
    refusal = error_of(response)
    assert response.status_code == 410
    assert refusal["retryable"] is False
    assert "retry_after_s" not in refusal
    assert "Retry-After" not in response.headers
    assert refusal["code"] != Code.SEGMENT_NOT_READY.value
    assert refusal["detail"]["state"] == JobState.CANCELED.value


# ======================================================================
# F-48 / F-49 -- state query and cancellation
# ======================================================================


def test_a_job_query_reports_state_progress_and_result_readiness(api: Api) -> None:
    job_id = accepted_job(api, text=KO)
    early = api.get(f"{API_PREFIX}/jobs/{job_id}").json()
    assert early["state"] == JobState.ACCEPTED.value
    assert early["result_ready"] is False
    assert early["progress"]["total_segments"] >= 1
    assert early["progress"]["generated_segments"] == 0
    assert early["client_label"] == "client"
    assert early["request_path"] == RequestPath.REST.value

    complete_job(api.engine, job_id)
    done = api.get(f"{API_PREFIX}/jobs/{job_id}").json()
    assert done["state"] == JobState.COMPLETE.value
    assert done["result_ready"] is True
    assert done["progress"]["fraction"] == 1.0
    assert done["audio_duration_ms"] > 0


def test_a_job_query_never_carries_the_source_text(api: Api) -> None:
    """F-56 makes the summary the default and puts the snapshot behind its own
    request, so the state query must not smuggle it out."""
    job_id = accepted_job(api, text=KO, retain=True)
    assert KO not in api.get(f"{API_PREFIX}/jobs/{job_id}").text
    assert KO not in api.get(f"{API_PREFIX}/jobs").text
    assert KO in api.get(f"{API_PREFIX}/jobs/{job_id}/text").text


def test_cancelling_a_running_job_is_accepted_with_202(api: Api) -> None:
    job_id = accepted_job(api)
    response = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel")
    assert response.status_code == 202
    assert api.engine.cancelled == [job_id]
    assert api.store.get_job(job_id).state is JobState.CANCELED


def test_cancelling_answers_promptly_even_when_release_takes_longer(api: Api) -> None:
    """N-22 gives cancellation one second at p95 and resource release five, so
    the answer must not wait for the worker to die."""
    job_id = accepted_job(api)
    api.engine.cancel_blocks_for_s = 2.0
    started = ids.monotonic()
    response = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel")
    elapsed = ids.monotonic() - started
    assert response.status_code == 202
    assert elapsed < 1.0, f"cancellation took {elapsed:.2f}s"


def test_cancelling_an_already_terminal_job_is_200_with_its_final_state(api: Api) -> None:
    job_id = accepted_job(api)
    complete_job(api.engine, job_id)
    response = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == JobState.COMPLETE.value
    assert api.engine.cancelled == []


def test_repeating_a_cancellation_adds_no_further_side_effects(api: Api) -> None:
    job_id = accepted_job(api)
    first = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel")
    second = api.post(f"{API_PREFIX}/jobs/{job_id}/cancel")
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["state"] == JobState.CANCELED.value
    assert api.engine.cancelled == [job_id]


def test_a_failed_job_reports_its_reason_as_the_jobs_state(api: Api) -> None:
    """5.3: generation errors after acceptance surface as the job's failure."""
    job_id = accepted_job(api)
    api.store.record_job_error(job_id, Code.WORKER_LOST, "The worker stopped.")
    api.store.update_job_state(job_id, JobState.FAILED)
    body = api.get(f"{API_PREFIX}/jobs/{job_id}").json()
    assert body["state"] == JobState.FAILED.value
    assert body["error"]["code"] == Code.WORKER_LOST.value
    assert body["error"]["retryable"] is is_retryable(Code.WORKER_LOST)


# ======================================================================
# F-56 -- history
# ======================================================================


def test_history_lists_only_the_callers_own_retained_jobs(api: Api) -> None:
    mine = accepted_job(api, retain=True)
    api.engine.release()
    theirs = accepted_job(api, actor="other", retain=True)
    api.engine.release()

    page = api.get(f"{API_PREFIX}/jobs").json()
    assert [item["job_id"] for item in page["items"]] == [mine]
    assert page["total"] == 1
    assert theirs not in api.get(f"{API_PREFIX}/jobs").text


def test_history_defaults_to_retained_jobs_and_can_be_asked_for_one_off_ones(api: Api) -> None:
    one_off = accepted_job(api, idempotency_key="a")
    api.engine.release()
    retained = accepted_job(api, idempotency_key="b", text=KO, retain=True)

    default_page = api.get(f"{API_PREFIX}/jobs").json()
    assert [item["job_id"] for item in default_page["items"]] == [retained]
    asked = api.get(f"{API_PREFIX}/jobs?retention=one_off").json()
    assert [item["job_id"] for item in asked["items"]] == [one_off]


def test_a_page_is_clamped_to_the_policy_maximum(api: Api) -> None:
    page = api.get(f"{API_PREFIX}/jobs?limit=5000").json()
    assert page["limit"] == LIST_PAGE_MAX
    assert page["offset"] == 0
    assert page["has_more"] is False


def test_history_can_be_filtered_by_state(api: Api) -> None:
    first = accepted_job(api, idempotency_key="a", retain=True)
    complete_job(api.engine, first, retained=True)
    accepted_job(api, idempotency_key="b", text=KO, retain=True)

    page = api.get(f"{API_PREFIX}/jobs?state=complete").json()
    assert [item["job_id"] for item in page["items"]] == [first]


def test_a_snapshot_that_is_gone_is_404_rather_than_an_empty_string(api: Api) -> None:
    job_id = accepted_job(api)
    api.store.clear_job_source_text(job_id)
    response = api.get(f"{API_PREFIX}/jobs/{job_id}/text")
    assert response.status_code == 404
    assert error_of(response)["code"] == Code.NOT_FOUND.value


# ======================================================================
# F-53 -- status and models
# ======================================================================


def test_status_reports_the_version_the_policy_and_the_granted_capabilities(api: Api) -> None:
    body = api.get(f"{API_PREFIX}/status", actor="generator").json()
    assert body["service"] == "echoact"
    assert body["api_version"] == "v1"
    assert body["state"] == "running"
    assert body["bind_host"] == REST_HOST and body["bind_port"] == PORT
    assert body["capabilities"]["granted"] == ["generate"]
    assert body["capabilities"]["generation"] is True
    assert body["capabilities"]["history"] is False
    policy = body["resource_policy"]
    assert policy["max_request_body_bytes"] == MAX_REQUEST_BODY_BYTES
    assert policy["concurrent_generation"] == 1
    assert policy["generation_requests_per_minute"] == RATE_GENERATION_PER_MIN
    assert policy["other_requests_per_minute"] == RATE_OTHER_PER_MIN
    assert policy["list_page_max"] == LIST_PAGE_MAX
    assert policy["bounded_wait_max_s"] == api.context.bounded_wait_ceiling_s


def test_the_owner_credential_is_granted_every_capability(api: Api) -> None:
    granted = api.get(f"{API_PREFIX}/status", actor="owner").json()["capabilities"]["granted"]
    assert set(granted) == {c.value for c in Capability}


def test_models_distinguishes_an_undownloaded_model_from_a_fault(api: Api) -> None:
    response = api.get(f"{API_PREFIX}/models")
    assert response.status_code == 200
    body = response.json()
    entry = next(m for m in body["models"] if m["model_id"] == SUPERTONIC_3_ID)
    assert entry["ready"] is False
    assert entry["state"] == "not_present"
    assert entry["bytes_total"] > 0
    assert entry["sample_rate"] == SR


def test_every_voice_carries_a_description_a_caller_can_choose_on(api: Api) -> None:
    """F-53: a bare identifier gives a caller that is not a person no basis
    for choosing a voice."""
    entry = next(
        m
        for m in api.get(f"{API_PREFIX}/models").json()["models"]
        if m["model_id"] == SUPERTONIC_3_ID
    )
    assert len(entry["voices"]) == 10
    for voice in entry["voices"]:
        assert voice["description"].strip()
        assert voice["display_name"].strip()
        assert voice["gender"] in {"male", "female"}


def test_models_states_the_input_formats_and_the_tempo_range(api: Api) -> None:
    body = api.get(f"{API_PREFIX}/models").json()
    assert body["input_formats"] == ["TXT", "Markdown"]
    assert body["tempo_min"] == 0.70 and body["tempo_max"] == 1.50
    assert set(body["styles"]) == {s.value for s in SpeakingStyle}


# ======================================================================
# 5.3 -- refusals that come from the engine
# ======================================================================


def test_a_model_that_is_not_prepared_is_refused_rather_than_accepted(api: Api) -> None:
    api.engine.refuse_with = EchoActError(
        Code.MODEL_NOT_READY, detail={"model_id": SUPERTONIC_3_ID}
    )
    response = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert response.status_code == 409
    body = error_of(response)
    assert body["code"] == Code.MODEL_NOT_READY.value
    assert body["retryable"] is False
    assert "Retry-After" not in response.headers


def test_insufficient_resources_answers_503_with_a_hint(api: Api) -> None:
    api.engine.refuse_with = EchoActError(Code.INSUFFICIENT_RESOURCES, retry_after_s=30.0)
    response = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert response.status_code == 503
    body = error_of(response)
    assert body["code"] == Code.INSUFFICIENT_RESOURCES.value
    assert body["retry_after_s"] == pytest.approx(30.0)
    assert response.headers["Retry-After"] == "30"


def test_a_shutting_down_engine_is_reported_and_not_masked(api: Api) -> None:
    api.engine.refuse_with = EchoActError(Code.SHUTTING_DOWN)
    response = api.post(f"{API_PREFIX}/jobs", json=create_body())
    assert response.status_code == 503
    assert error_of(response)["code"] == Code.SHUTTING_DOWN.value


# ======================================================================
# F-52 / F-79 -- the service's own lifetime
# ======================================================================


def test_draining_blocks_new_requests(api: Api) -> None:
    assert api.get(f"{API_PREFIX}/status").status_code == 200
    api.context.drain()
    response = api.get(f"{API_PREFIX}/status")
    assert response.status_code == 503
    assert error_of(response)["code"] == Code.SERVICE_OFF.value


def test_stopping_blocks_requests_then_cancels_this_integrations_job() -> None:
    """F-52's order, and only this integration's jobs."""
    application = FakeApplication()
    issued = application.credentials.issue(name="rest", capabilities={Capability.GENERATE})
    runner = ServiceRunner(application, port=_free_port())
    runner.start()
    try:
        with TestClient(
            create_app(runner.context),
            base_url=f"http://{REST_HOST}:{runner.port}",
            raise_server_exceptions=False,
        ) as client:
            headers = {"Authorization": f"Bearer {issued.token}"}
            created = client.post(
                f"{API_PREFIX}/jobs", json=create_body(), headers=headers
            )
            assert created.status_code == 202
            job_id = created.json()["job_id"]

            runner.stop()
            assert application.engine.cancelled == [job_id]
            assert application.store.get_job(job_id).state is JobState.CANCELED
            refused = client.get(f"{API_PREFIX}/status", headers=headers)
            assert refused.status_code == 503
    finally:
        runner.stop()
        application.close()


def test_stopping_leaves_a_gui_job_alone() -> None:
    """F-52: GUI jobs are unaffected when an integration is turned off."""
    application = FakeApplication()
    request = JobRequest(
        text=EN,
        settings=_voice_settings(),
        request_path=RequestPath.GUI,
        owner_client_id="gui",
        idempotency_key="gui-1",
    )
    job, created = application.engine.submit(request)
    assert created

    runner = ServiceRunner(application, port=_free_port())
    runner.start()
    runner.stop()
    assert application.engine.cancelled == []
    assert application.store.get_job(job.job_id).state is JobState.ACCEPTED
    application.close()


def test_a_bind_failure_names_the_port_and_never_takes_another() -> None:
    """F-79: the port conflict is reported, no alternative is bound, and the
    application keeps running."""
    application = FakeApplication()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind((REST_HOST, 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    runner = ServiceRunner(application, port=taken)
    try:
        with pytest.raises(EchoActError) as caught:
            runner.start()
        assert caught.value.code is Code.SERVICE_PORT_UNAVAILABLE
        assert caught.value.detail["port"] == taken
        assert caught.value.detail["host"] == REST_HOST
        assert runner.running is False
    finally:
        holder.close()
        application.close()


def test_the_runner_binds_the_loopback_address_and_nothing_else() -> None:
    """N-17: binding a non-loopback address is not configurable, so no setting
    and no argument can produce one."""
    application = FakeApplication()
    application.settings = application.settings.with_(rest_port=_free_port())
    runner = ServiceRunner(application)
    assert runner.host == REST_HOST
    runner.start()
    try:
        assert runner.running is True
        assert runner.port == application.settings.rest_port
    finally:
        runner.stop()
        application.close()


def test_a_running_service_answers_over_a_real_socket() -> None:
    """The whole path, once: a real listener on the loopback address, a real
    HTTP request, and a real credential."""
    import httpx

    application = FakeApplication()
    issued = application.credentials.issue(name="probe", capabilities={Capability.GENERATE})
    runner = ServiceRunner(application, port=_free_port())
    runner.start()
    try:
        response = httpx.get(
            f"http://{REST_HOST}:{runner.port}{API_PREFIX}/status",
            headers={"Authorization": f"Bearer {issued.token}"},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert response.json()["bind_port"] == runner.port
        unauthenticated = httpx.get(
            f"http://{REST_HOST}:{runner.port}{API_PREFIX}/status", timeout=10.0
        )
        assert unauthenticated.status_code == 401
    finally:
        runner.stop()
        application.close()
    assert runner.running is False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((REST_HOST, 0))
        return int(probe.getsockname()[1])
