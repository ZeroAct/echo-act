"""F-75's release check: one narrow question, honestly answered.

The transport is a mock because the point is the module's contract --
what it asks, what it accepts, and how it fails -- not PyPI's uptime.
What the test does pin, at the wire level, is the promise behind N-01
and N-20: the request is a GET to the feed with nothing of the owner's
in it.
"""

from __future__ import annotations

import httpx
import pytest

from echoact import update
from echoact.errors import Code, EchoActError


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_the_feed_names_the_newest_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {"version": "9.9.9"}})

    assert update.latest_released_version(transport=_transport(handler)) == "9.9.9"


def test_the_query_is_a_bare_get_with_nothing_of_the_owner_in_it() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"info": {"version": "0.2.0"}})

    update.latest_released_version(transport=_transport(handler))
    (request,) = seen
    assert request.method == "GET"
    assert request.url.host == "pypi.org"
    assert request.url.path == "/pypi/echoact/json"
    assert request.content == b""


def test_a_refused_feed_fails_as_one_honest_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(EchoActError) as raised:
        update.latest_released_version(transport=_transport(handler))
    assert raised.value.code is Code.RELEASE_FEED_UNREACHABLE
    assert raised.value.message  # the screen shows the reason, not just a code


def test_a_body_without_a_version_is_not_guessed_at() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"info": {}})

    with pytest.raises(EchoActError) as raised:
        update.latest_released_version(transport=_transport(handler))
    assert raised.value.code is Code.RELEASE_FEED_UNREACHABLE
