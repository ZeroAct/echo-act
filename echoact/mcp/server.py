"""The MCP server: eight tools, each one REST call, and nothing else.

F-59 is unusually strict about this and it is worth restating, because the
temptation to be helpful here is what would break it: each tool maps onto
one REST operation and adds no capability REST does not already expose.
N-24 makes the REST contract canonical and MCP a projection of it, so a
behaviour difference between the two is a defect -- which is why these
tools pass the service's JSON through rather than re-modelling it.  A
second model of the same data is a second thing to keep in step, and it
would not stay in step.

The revision this implements, 2026-07-28, is stateless and has no
connection-establishing handshake, so:

* Nothing is cached between calls and no session is opened.  The
  connection is read once at start-up because it is configuration, not
  state.
* Cross-call state travels as a server-minted job identifier passed as an
  ordinary tool argument -- which is what ``job_id`` is.
* Result references are MCP resource URIs, and they are identifiers
  rather than addresses.  ``echoact://job/<id>/audio`` names a thing; it
  carries no host, no filesystem path, and no credential, and resolving
  it requires this process's own credential.  §2.11 forbids the
  alternatives by name.
* Resource reads are cacheable on this revision, so every reference
  carries a freshness hint no longer than the result's remaining lifetime
  and is marked private, so no shared intermediary may hold one owner's
  audio.  The hint travels on the reference the tool returns.  The
  protocol's own ``ttlMs``/``cacheScope`` on a read default to 0 and
  private in this SDK, which is stricter than the requirement rather than
  looser, so the two agree and neither has to be argued about.

Diagnostics go to stderr.  The revision deprecates the protocol's own
logging and §2.11 says so explicitly.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from ..errors import Code, EchoActError
from ..policy import BOUNDED_WAIT_CEILING_S, MCP_PROTOCOL_REVISION
from ..util import ids
from .client import RestClient, describe
from .config import Connection

SERVER_NAME = "echoact"
RESOURCE_SCHEME = "echoact"

INSTRUCTIONS = """\
EchoAct generates Korean and English speech locally, on this machine.

One generation runs at a time across the whole application, including the
person sitting at it, so contention is normal: a busy answer carries a
retry_after_s and giving up is a reasonable response to it.

Call estimate_speech before create_speech for anything long: it validates,
counts segments, gives the expected length, and says whether the slot is
free, without creating a job or loading a model.

create_speech needs an idempotency_key. Reuse the same key when you retry
and you will get the same job back rather than a second rendering.
"""


def _stderr(message: str) -> None:
    print(f"[echoact-mcp] {message}", file=sys.stderr, flush=True)


def audio_uri(job_id: str, segment_id: str | None = None) -> str:
    if segment_id:
        return f"{RESOURCE_SCHEME}://job/{job_id}/segment/{segment_id}"
    return f"{RESOURCE_SCHEME}://job/{job_id}/audio"


def _ttl_ms(result: dict[str, Any], now: float | None = None) -> int:
    """A freshness hint that cannot outlive the result.

    F-60 caps the hint at the remaining lifetime in 4.1.  A retained
    result has no expiry, and a hint that never goes stale would still be
    wrong -- the owner may delete it -- so it is capped at the one-hour
    figure the same section uses for a one-off.
    """
    moment = ids.now() if now is None else now
    expires = result.get("expires_at")
    if expires is None:
        return 3_600_000
    remaining = float(expires) - moment
    return max(0, int(remaining * 1000))


def build(connection: Connection, *, client: RestClient | None = None):
    """Construct the server.  Takes a client so a test can supply one."""
    from fastmcp import FastMCP

    rest = client or RestClient(connection)
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    def call(fn):
        """Run one REST call, and turn a failure into a described result.

        A tool that raised would give the client an exception where F-57
        promises a code, whether a retry can succeed, and a request
        identifier.  So failures come back as data.
        """
        try:
            return fn()
        except EchoActError as exc:
            if exc.code in {Code.APP_NOT_RUNNING, Code.SERVICE_OFF, Code.MCP_DISABLED}:
                _stderr(f"{exc.code.value}: {exc.message}")
            return describe(exc)

    # -- discovery ------------------------------------------------------

    @mcp.tool
    def list_models() -> dict[str, Any]:
        """Models, voices, languages, speaking styles and input limits.

        Every voice carries a description as well as an identifier,
        because an identifier alone gives a caller that is not a person
        nothing to choose on (F-53).
        """
        return call(lambda: rest.get("/models"))

    @mcp.tool
    def estimate_speech(
        text: Annotated[str, "The text to be spoken."],
        voice_id: Annotated[str | None, "A voice from list_models."] = None,
        language: Annotated[str | None, "auto, ko or en."] = None,
        style: Annotated[str | None, "natural, calm, bright or narration."] = None,
        tempo: Annotated[float | None, "0.70 to 1.50."] = None,
    ) -> dict[str, Any]:
        """Validate and estimate without creating a job or loading a model.

        Answers with the segment count, the expected audio length and
        synthesis time, and whether the single generation slot is free.
        The figures are approximate by construction: they come from the
        model's recorded throughput, not from a trial run (F-88).
        """
        body = _settings_body(text=text, voice_id=voice_id, language=language, style=style, tempo=tempo)
        return call(lambda: rest.post("/estimate", body))

    # -- generation -----------------------------------------------------

    @mcp.tool
    def create_speech(
        text: Annotated[str, "The text to be spoken. Files and URLs are not accepted."],
        idempotency_key: Annotated[
            str, "Required. Reuse it on a retry to get the same job back rather than a second one."
        ],
        voice_id: Annotated[str | None, "A voice from list_models."] = None,
        language: Annotated[str | None, "auto, ko or en."] = None,
        style: Annotated[str | None, "natural, calm, bright or narration."] = None,
        tempo: Annotated[float | None, "0.70 to 1.50."] = None,
        wait_seconds: Annotated[
            float | None,
            "Wait up to this long for the job to finish before answering. "
            "The job is untouched either way; if the bound passes you get the job id.",
        ] = None,
        retain: Annotated[bool, "Keep the result in EchoAct's library."] = False,
    ) -> dict[str, Any]:
        """Start generating speech.

        Returns the job's state.  If it finished inside ``wait_seconds``
        the answer is terminal; otherwise it carries the job id and the
        job continues untouched.  Waiting never covers model preparation.

        File and URL inputs are not accepted: N-18 keeps arbitrary paths
        and external URLs out of what an integration can ask EchoAct to
        read.
        """
        body = _settings_body(
            text=text, voice_id=voice_id, language=language, style=style, tempo=tempo
        )
        body["kind"] = "speech"
        body["idempotency_key"] = idempotency_key
        body["retention"] = "retained" if retain else "one_off"
        if wait_seconds is not None:
            body["wait_s"] = max(0.0, min(float(wait_seconds), BOUNDED_WAIT_CEILING_S))
        return call(lambda: rest.post("/jobs", body))

    @mcp.tool
    def get_speech_job(job_id: Annotated[str, "From create_speech."]) -> dict[str, Any]:
        """State, progress and any error for one of your own jobs."""
        return call(lambda: rest.get(f"/jobs/{job_id}"))

    @mcp.tool
    def cancel_speech_job(job_id: Annotated[str, "From create_speech."]) -> dict[str, Any]:
        """Cancel one of your own jobs.

        Cancelling twice adds nothing, and cancelling a job that already
        finished returns its final state rather than changing it.  This is
        not the same as cancelling the MCP request itself.
        """
        return call(lambda: rest.post(f"/jobs/{job_id}/cancel"))

    @mcp.tool
    def list_speech_segments(job_id: Annotated[str, "From create_speech."]) -> dict[str, Any]:
        """Ready segments, with their source-text range and their times.

        Ranges are Unicode code point offsets into the text you supplied,
        start inclusive and end exclusive, and times are milliseconds from
        the start of the audio.
        """
        return call(lambda: rest.get(f"/jobs/{job_id}/segments"))

    @mcp.tool
    def get_speech_result(
        job_id: Annotated[str, "From create_speech."],
        segment_id: Annotated[str | None, "From list_speech_segments, for one segment."] = None,
    ) -> dict[str, Any]:
        """A reference to the finished audio, and what it is.

        The audio is not embedded here.  You get a resource URI to read
        over this same session, plus the format, rate, size, length and
        expiry.  The URI is an identifier, not an address: it holds no
        path and no credential, and only this server can resolve it.
        """

        def fetch() -> dict[str, Any]:
            result = rest.get(f"/jobs/{job_id}/result")
            uri = audio_uri(job_id, segment_id)
            ttl = _ttl_ms(result)
            return {
                **result,
                "resource": {
                    "uri": uri,
                    "mime_type": "audio/wav",
                    # F-60: no longer than the remaining lifetime, and
                    # private so no shared cache may hold one owner's audio.
                    "ttl_ms": ttl,
                    "cache_scope": "private",
                },
            }

        return call(fetch)

    @mcp.tool
    def list_speech_history(
        limit: Annotated[int | None, "Up to 100."] = None,
        model: Annotated[str | None, "Filter by model id."] = None,
        state: Annotated[str | None, "Filter by job state."] = None,
    ) -> dict[str, Any]:
        """Your own retained jobs, if your credential may read history.

        A summary without body text is what you get; the source text of a
        retained job needs its own permission and its own request.
        """
        return call(lambda: rest.get("/jobs", limit=limit, model=model, state=state))

    # -- resources ------------------------------------------------------

    @mcp.resource(
        f"{RESOURCE_SCHEME}://job/{{job_id}}/audio",
        name="Finished audio",
        mime_type="audio/wav",
        description="The complete WAV for a job, resolved on your behalf.",
    )
    def job_audio(job_id: str) -> bytes:
        payload, _ = rest.get_bytes(f"/jobs/{job_id}/audio")
        return payload

    @mcp.resource(
        f"{RESOURCE_SCHEME}://job/{{job_id}}/segment/{{segment_id}}",
        name="Segment audio",
        mime_type="audio/wav",
        description="One ready segment's WAV, resolved on your behalf.",
    )
    def segment_audio(job_id: str, segment_id: str) -> bytes:
        payload, _ = rest.get_bytes(f"/jobs/{job_id}/segments/{segment_id}/audio")
        return payload

    return mcp


def _settings_body(
    *,
    text: str,
    voice_id: str | None,
    language: str | None,
    style: str | None,
    tempo: float | None,
) -> dict[str, Any]:
    """Only what the caller actually named.

    Omitting a field lets the service apply the owner's own setting, which
    is the behaviour F-47 describes -- all entry paths share one set of
    choices.  Sending a default from here would silently override the
    owner instead.
    """
    body: dict[str, Any] = {"text": text}
    for name, value in (
        ("voice_id", voice_id),
        ("language", language),
        ("style", style),
        ("tempo", tempo),
    ):
        if value is not None:
            body[name] = value
    return body


def preflight(rest: RestClient) -> dict[str, Any]:
    """Check that this process can be useful before serving anything.

    F-52 requires an app that is not running, a REST service that is off,
    and a disabled MCP integration to be reported to the client as
    distinct, non-retrying errors -- and requires this process never to
    launch the app.  Doing it here means the client is told once, clearly,
    rather than on every tool call.
    """
    status = rest.get("/status")
    if not status.get("mcp_enabled", True):
        raise EchoActError(
            Code.MCP_DISABLED,
            "MCP is turned off in EchoAct. Enable it under Settings; "
            "this server cannot enable it.",
        )
    revision = status.get("mcp_protocol_revision")
    if revision and revision != MCP_PROTOCOL_REVISION:
        _stderr(
            f"EchoAct reports MCP revision {revision}; this server implements "
            f"{MCP_PROTOCOL_REVISION}."
        )
    return status
