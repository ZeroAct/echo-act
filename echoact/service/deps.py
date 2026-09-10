"""The gate every request passes through, and the helpers the routes share.

Section 2.10 and N-17 fix an order, and this module is where that order is
actually written down:

1. **Host and Origin**, before anything else.  N-17 says allowed values are
   "validated before authentication is evaluated", so the check cannot live in
   a route dependency -- dependencies run after the request has been matched
   and, in FastAPI, alongside body parsing.  It is the first thing the gate
   does, and it is the first thing that can refuse.
2. **Service state.**  F-52 requires turning an integration off to block new
   requests before anything is torn down, so a draining service refuses here
   rather than letting a request reach an engine that is about to stop.
3. **Body size.**  4.1 caps a request body at 2,000,000 bytes including upload
   metadata.  ``Content-Length`` is refused before the body is read at all,
   and a chunked body is counted as it arrives, so the cap is enforced before
   the bytes are in memory rather than after.
4. **Authentication**, always, including on a first launch (N-31).  It runs in
   the gate rather than per route so that there is no path -- not an unknown
   URL, not the specification document -- that answers anything to an
   unauthenticated caller.
5. **Rate limits** (4.1), per client, once the client is known.

Capability and ownership are *not* here: they differ per operation, so each
route asks for what it needs through :func:`require_job` and
:func:`require_capability`.  Both go through ``CredentialStore.authorise``,
which re-resolves the credential against the live store, so a revocation that
lands mid-request applies to the rest of it (F-71).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

from ..domain import Capability, Job
from ..errors import Code, EchoActError
from ..paths import audio_dir, temp_dir
from ..policy import (
    ALLOWED_HOSTS,
    API_PREFIX,
    BOUNDED_WAIT_CEILING_S,
    BOUNDED_WAIT_DEFAULT_S,
    MAX_REQUEST_BODY_BYTES,
    REST_HOST,
)
from ..security.credentials import Credential
from ..security.ratelimit import AuthFailureLimiter, RequestClass
from ..util import ids
from ..util.logging import get_logger
from .errors import REQUEST_ID_HEADER, error_response

log = get_logger("service.gate")

#: Bytes handed to the transport per read while streaming audio.  N-21 forbids
#: loading a result into memory -- two hours of 44.1 kHz mono is about 635 MB
#: -- and 64 KiB is large enough that the syscall count is irrelevant beside
#: the disk read.
AUDIO_CHUNK_BYTES = 1 << 16

#: Longest this service will hold a cancellation request open before answering
#: 202.  N-22 gives cancellation one second at p95 and gives *resource release*
#: five, so the two are deliberately different waits: the caller is told the
#: cancellation was accepted, and the worker is killed on another thread.
CANCEL_ACK_GRACE_S = 0.25

_SCHEMES = ("http", "https")


# ======================================================================
# Context
# ======================================================================


class ServiceContext:
    """What the routes may reach, and the service's own lifetime state.

    Everything is read through the ``Application`` rather than copied out of
    it, because ``Application.update_settings`` replaces the settings object:
    a service holding its own snapshot would answer F-53's "actual resource
    policy" with the policy that applied when it started.
    """

    def __init__(
        self,
        application: Any,
        *,
        port: int | None = None,
        bounded_wait_ceiling_s: float = BOUNDED_WAIT_DEFAULT_S,
        auth_failures: AuthFailureLimiter | None = None,
    ) -> None:
        self.application = application
        self._port = port
        self._ceiling_s = max(0.0, min(float(bounded_wait_ceiling_s), BOUNDED_WAIT_CEILING_S))
        self.auth_failures = auth_failures or AuthFailureLimiter()
        self.started_at = ids.now()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="echoact-service")
        self._draining = False

    # -- the parts ------------------------------------------------------

    @property
    def store(self) -> Any:
        return self.application.store

    @property
    def engine(self) -> Any:
        return self.application.engine

    @property
    def registry(self) -> Any:
        return self.application.registry

    @property
    def credentials(self) -> Any:
        return self.application.credentials

    @property
    def limiter(self) -> Any:
        return self.application.limiter

    @property
    def settings(self) -> Any:
        return self.application.settings

    @property
    def manifest(self) -> Any:
        return self.registry.manifest

    # -- service state --------------------------------------------------

    @property
    def host(self) -> str:
        """N-17: the loopback address, and no other.

        A property rather than a field so that nothing -- not a setting, not a
        constructor argument -- can substitute another value.
        """
        return REST_HOST

    @property
    def port(self) -> int:
        return int(self._port if self._port is not None else self.settings.rest_port)

    @property
    def bounded_wait_ceiling_s(self) -> float:
        """4.1's ceiling on F-88's wait, in seconds.

        4.1 allows a request to ask for up to ten seconds and lets the owner
        raise the ceiling to sixty.  ``Settings`` has no field for that choice
        yet, so the default is the ten-second bound and the constructor takes
        the owner's value; either way it is clamped to the sixty-second cap,
        because the service caps what the caller asked for as well as what the
        owner allowed.
        """
        return self._ceiling_s

    @property
    def draining(self) -> bool:
        return self._draining

    def drain(self) -> None:
        """F-52's first step: block new requests.

        Separate from stopping the listener so that the two other steps --
        cancelling this integration's jobs, then shutting down -- happen with
        nothing new arriving behind them.
        """
        self._draining = True

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


# ======================================================================
# Header validation (N-17)
# ======================================================================


def split_host_port(raw: str) -> tuple[str, int | None]:
    """Split an authority into host and port, IPv6 literals included.

    ``rsplit`` alone mis-parses ``[::1]:8765`` and ``::1`` both, so the
    bracketed form is handled first and a bare IPv6 address -- which has more
    than one colon and therefore no port -- is left whole.
    """
    value = raw.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return value, None
        host = value[1 : closing]
        rest = value[closing + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, int(port) if port.isdigit() else None
    return value, None


def check_host(raw: str | None, port: int) -> None:
    """N-17's Host check.

    A missing Host is refused rather than tolerated: HTTP/1.1 requires one and
    HTTP/2's ``:authority`` becomes one, so its absence means a client that is
    not speaking either -- and accepting it would leave a hole exactly where
    this check is supposed to be.  A port that is not this listener's is
    refused too, since a request that believes it reached a different service
    is the shape a rebinding attack arrives in.
    """
    if not raw:
        raise EchoActError(Code.HOST_NOT_ALLOWED, "The request carried no Host header.")
    host, claimed_port = split_host_port(raw)
    if host.lower() not in ALLOWED_HOSTS:
        raise EchoActError(Code.HOST_NOT_ALLOWED, detail={"allowed": sorted(ALLOWED_HOSTS)})
    if claimed_port is not None and claimed_port != port:
        raise EchoActError(Code.HOST_NOT_ALLOWED, detail={"expected_port": port})


def check_origin(raw: str | None, port: int) -> None:
    """N-17's Origin check, with CORS disallowed by default.

    No Origin header is the ordinary case: an automated client is not a
    browser and sends none.  When one *is* present the request came from a
    page, and the only page this service will answer is one served from its
    own origin -- which, since nothing here serves pages, is in practice none
    at all.  Anything else is refused before authentication, and no
    ``Access-Control-Allow-Origin`` is ever emitted, so a browser could not
    read the answer even if it got one.
    """
    if raw is None:
        return
    value = raw.strip()
    scheme, sep, authority = value.partition("://")
    if not sep or scheme.lower() not in _SCHEMES:
        raise EchoActError(Code.ORIGIN_NOT_ALLOWED, detail={"cors": "disallowed"})
    host, claimed_port = split_host_port(authority)
    if host.lower() not in ALLOWED_HOSTS or claimed_port != port:
        raise EchoActError(Code.ORIGIN_NOT_ALLOWED, detail={"cors": "disallowed"})


def classify(method: str, path: str) -> RequestClass:
    """4.1 counts generation requests apart from everything else.

    Only creating a job is a generation request.  An estimate deliberately is
    not: F-88 says it creates no job, loads no model, and synthesises nothing,
    so charging it against the ten-per-minute generation allowance would make
    the pre-flight that exists to avoid wasted work cost as much as the work.
    """
    if method == "POST" and path.rstrip("/") == f"{API_PREFIX}/jobs":
        return RequestClass.GENERATION
    return RequestClass.OTHER


# ======================================================================
# The gate
# ======================================================================


class ServiceGate:
    """One pure-ASGI layer holding N-17's order.

    Written as a single layer rather than four stacked middlewares for two
    reasons.  The order is the requirement, and one function is where a
    reviewer can read it; and each ``BaseHTTPMiddleware`` adds a task group and
    a queue per request, which N-22's one-second p95 does not need to pay for
    four times over.
    """

    def __init__(self, app: Any, *, context: ServiceContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Section 2.10 is twelve HTTP operations.  A WebSocket upgrade has
            # no place in it, and answering one would be a surface nothing in
            # the contract describes.
            await _reject_non_http(scope, receive, send)
            return

        request_id = ids.request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        send = _with_request_id(send, request_id)

        try:
            credential, guarded_receive = self._admit(scope, receive)
        except EchoActError as exc:
            log.info(
                "%s %s -> %d %s (request=%s)",
                scope.get("method", "?"),
                scope.get("path", "?"),
                exc.http_status,
                exc.code.value,
                request_id,
            )
            await error_response(exc, request_id)(scope, receive, send)
            return

        state["credential"] = credential
        await self.app(scope, guarded_receive, send)

    # -- the order ------------------------------------------------------

    def _admit(self, scope: dict[str, Any], receive: Any) -> tuple[Credential, Any]:
        headers = _headers(scope)
        port = self.context.port

        check_host(headers.get("host"), port)
        check_origin(headers.get("origin"), port)

        if self.context.draining:
            raise EchoActError(Code.SERVICE_OFF, "The local service is shutting down.")

        declared = headers.get("content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > MAX_REQUEST_BODY_BYTES:
                raise EchoActError(
                    Code.PAYLOAD_TOO_LARGE,
                    detail={"limit_bytes": MAX_REQUEST_BODY_BYTES, "declared": int(declared)},
                )

        credential = self._authenticate(scope, headers)
        self.context.limiter.raise_if_limited(
            credential.client_id, classify(scope.get("method", ""), scope.get("path", ""))
        )
        return credential, _body_limited(receive)

    def _authenticate(self, scope: dict[str, Any], headers: dict[str, str]) -> Credential:
        """N-31's fail-closed authentication.

        Every outcome that is not a valid credential counts against 4.1's
        authentication-failure limit for this origin, a missing header
        included: "authentication failed" is what the caller did, and treating
        an absent header as merely uninteresting would leave the cheapest
        possible probe uncounted.  The lockout blocks that origin and nothing
        else -- 4.1 is explicit that neither the service nor the GUI stops.

        The attempt that reaches the limit is still answered 401.  4.1 blocks
        authentication *retries*, so the failure that trips the lockout is
        reported as what it was, and the block begins with the next request --
        which is also why the lockout is checked before the credential is
        looked at rather than after it fails.
        """
        origin = _peer(scope)
        self.context.auth_failures.raise_if_locked(origin)

        token = _bearer(headers.get("authorization"))
        if token is None:
            self.context.auth_failures.record_failure(origin)
            raise EchoActError(Code.UNAUTHENTICATED)
        try:
            credential = self.context.credentials.authenticate(token)
        except EchoActError as exc:
            self.context.auth_failures.record_failure(origin)
            raise exc
        self.context.auth_failures.record_success(origin)
        return credential


async def _reject_non_http(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    raise EchoActError(Code.INTERNAL, "Unsupported connection type.")


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").lower()
        # First value wins.  A request presenting two Host headers is
        # ambiguous, and taking the first means a smuggled second one cannot
        # be the one that passes the check.
        out.setdefault(name, raw_value.decode("latin-1"))
    return out


def _peer(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


def _bearer(raw: str | None) -> str | None:
    """Extract a bearer token.  N-17 keeps tokens out of URLs, so the header
    is the only place one is ever read from."""
    if not raw:
        return None
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _with_request_id(send: Any, request_id: str) -> Any:
    async def wrapped(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message).setdefault(REQUEST_ID_HEADER, request_id)
        await send(message)

    return wrapped


def _body_limited(receive: Any) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Count the body as it arrives and refuse once it passes 4.1's cap.

    The ``Content-Length`` check above catches the ordinary case before a
    single byte is read.  This covers a chunked body, which declares no length
    at all: without it, "capped at 2,000,000 bytes" would hold only for
    clients that volunteer their size.
    """
    seen = 0

    async def wrapped() -> dict[str, Any]:
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > MAX_REQUEST_BODY_BYTES:
                raise EchoActError(
                    Code.PAYLOAD_TOO_LARGE, detail={"limit_bytes": MAX_REQUEST_BODY_BYTES}
                )
        return message

    return wrapped


# ======================================================================
# Per-route access checks (F-50, F-61, N-19)
# ======================================================================


def credential_of(request: Request) -> Credential:
    credential = request.scope.get("state", {}).get("credential")
    if not isinstance(credential, Credential):
        # Unreachable through the gate; a programming error if a route is
        # ever mounted outside it, and a 401 is the safe way to be wrong.
        raise EchoActError(Code.UNAUTHENTICATED)
    return credential


def require_capability(context: ServiceContext, request: Request, capability: Capability) -> Credential:
    """F-61's separately granted permissions, re-checked against the store.

    Re-resolving rather than trusting the object the gate produced is what
    makes F-71's "revocation applies immediately, including to result
    retrieval" true for a request that is already running.
    """
    credential = credential_of(request)
    context.credentials.authorise(credential, None, capability)
    return credential


def require_job(
    context: ServiceContext,
    request: Request,
    job_id: str,
    capability: Capability,
    *,
    include_text: bool = False,
    include_segments: bool = False,
) -> tuple[Credential, Job]:
    """N-19, in the one place every job route goes through.

    Knowing a job identifier grants nothing: the capability is checked first,
    then the job is fetched, then ownership.  A job owned by someone else
    answers 404 and not 403, which Section 2.10 states outright and which is
    also what stops a client discovering identifiers by asking about them --
    the answer is identical whether or not the job exists.
    """
    credential = require_capability(context, request, capability)
    job = context.store.get_job(
        job_id, include_source_text=include_text, include_segments=include_segments
    )
    context.credentials.authorise(credential, job.owner_client_id, capability)
    return credential, job


def owner_scope(credential: Credential) -> str | None:
    """Which owner's rows a list query may see.

    ``None`` means every owner, and only the GUI owner gets it: F-50 lets the
    owner review all jobs and confines every other client to its own.
    """
    return None if credential.is_owner else credential.client_id


# ======================================================================
# Managed audio (N-18, N-21)
# ======================================================================


def resolve_managed_audio(context: ServiceContext, stored_path: str | None) -> Path | None:
    """Turn a stored audio path into a file this service may read.

    A path is accepted only if it resolves inside one of the app's own audio
    roots.  N-18 forbids reading arbitrary paths on an external request, and a
    stored row is precisely where a path from a restored or tampered database
    would arrive; treating anything outside the tree as missing keeps that
    from becoming a file-read primitive.  ``None`` is returned rather than
    raised so the caller can answer 410 with the reason.
    """
    if not stored_path:
        return None
    candidate_path = Path(stored_path)
    for root in _audio_roots(context):
        try:
            resolved = (root / candidate_path).resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        try:
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def _audio_roots(context: ServiceContext) -> tuple[Path, ...]:
    """Retained audio, and the scratch tree a one-off result lives in.

    Both are needed: 4.1 keeps an external job's result retrievable for an
    hour after it ends, and a one-off result is written to the scratch tree
    that N-02 clears on relaunch rather than into retained storage.
    """
    roots = [Path(context.store.audio_root), audio_dir(), temp_dir()]
    seen: dict[str, Path] = {}
    for root in roots:
        seen.setdefault(os.path.normcase(str(root)), root)
    return tuple(seen.values())


def iter_file(path: Path, chunk_bytes: int = AUDIO_CHUNK_BYTES) -> Iterator[bytes]:
    """Stream a file to the transport in bounded blocks.

    N-21 forbids unbounded in-memory loading for result retrieval, and a
    two-hour result is roughly 635 MB, so the whole file is never held.  The
    bytes are sent exactly as stored -- header included -- because F-82 fixes
    the file's format and re-encoding it here would be a second definition of
    what a result is.
    """
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                return
            yield block
