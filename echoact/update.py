"""F-75: the one request the app makes on its owner's behalf.

Everything EchoAct does is local (N-01); this module is the single
exception and the narrowest one: a version query, at the owner's explicit
request, carrying no identifier, no document, and no text (N-20 has
nothing to redact because there is nothing to send).  It is separate from
``models/`` because that module downloads artefacts against a manifest and
verifies them (F-84); this one answers a question and keeps the answer for
exactly one screen repaint.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from .errors import Code, EchoActError
from .policy import RELEASE_CHECK_TIMEOUT_S

#: EchoAct is distributed from PyPI (N-10), so PyPI's index is the release
#: feed: one document, no version in the URL, and the latest release is
#: what its ``info.version`` names.
RELEASE_FEED_URL: Final = "https://pypi.org/pypi/echoact/json"


def latest_released_version(
    timeout_s: float = RELEASE_CHECK_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Ask the release feed for the newest published version.

    Any answer the app cannot show -- refusal, timeout, a body without a
    version -- leaves as ``RELEASE_FEED_UNREACHABLE`` with the reason in
    the message, because the screen's job is to say *why* nothing was
    found rather than to have checked and revealed nothing.
    """
    try:
        # A Client, not httpx.get(): the convenience function takes no
        # transport, and the transport is how this query is testable at
        # the wire level.
        with httpx.Client(
            timeout=timeout_s, follow_redirects=True, transport=transport
        ) as client:
            response = client.get(RELEASE_FEED_URL)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        version = body["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EchoActError(Code.RELEASE_FEED_UNREACHABLE, f"{type(exc).__name__}") from exc
    if not isinstance(version, str) or not version.strip():
        raise EchoActError(Code.RELEASE_FEED_UNREACHABLE, "the release feed named no version")
    return version.strip()


__all__ = ["RELEASE_FEED_URL", "latest_released_version"]
