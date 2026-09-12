"""F-89's gate: which play requests are refused, and why.

The gate is the half of external playback that has to answer a client
synchronously, so it is the half with the rules in it. The window's half is
tested where the window is; here the speaker is a stub with a state, because
what is under test is a decision and not a sound.
"""

from __future__ import annotations

import pytest

from echoact.audio.player import PlayerState
from echoact.config.settings import Settings
from echoact.errors import Code, EchoActError
from echoact.playrequests import PlayRequest, PlayRequests
from echoact.policy import PLAYBACK_BUSY_RETRY_AFTER_S


class FakePlayer:
    def __init__(self, state: PlayerState = PlayerState.STOPPED) -> None:
        self.state = state


def make(state: PlayerState = PlayerState.STOPPED, *, allowed: bool = True):
    player = FakePlayer(state)
    requests = PlayRequests(player, Settings(external_play=allowed))
    seen: list[PlayRequest] = []
    requests.listen(seen.append)
    return requests, player, seen


def test_a_request_reaches_the_window_when_nobody_is_listening() -> None:
    requests, _player, seen = make()
    requests.request("job_1", client_label="Claude Desktop")
    assert [(r.job_id, r.client_label) for r in seen] == [("job_1", "Claude Desktop")]


@pytest.mark.parametrize("state", [PlayerState.PLAYING, PlayerState.WAITING])
def test_the_owner_listening_refuses_the_request_with_a_hint(state: PlayerState) -> None:
    """F-50: the owner is not interrupted. F-57: and the client is told when
    to come back rather than left to guess a backoff."""
    requests, _player, seen = make(state)
    with pytest.raises(EchoActError) as caught:
        requests.request("job_1")
    assert caught.value.code is Code.PLAYBACK_BUSY
    assert caught.value.retry_after_s == PLAYBACK_BUSY_RETRY_AFTER_S
    assert seen == []


@pytest.mark.parametrize("state", [PlayerState.PAUSED, PlayerState.STOPPED, PlayerState.ENDED])
def test_playback_that_is_not_running_does_not_hold_the_speaker(state: PlayerState) -> None:
    """Section 5.2 distinguishes these states, and only the audible ones are
    someone listening. A job paused an hour ago is not."""
    requests, _player, seen = make(state)
    requests.request("job_1")
    assert len(seen) == 1


def test_the_setting_being_off_is_a_permanent_refusal() -> None:
    """N-23: nothing about trying again would change it, so it carries no
    hint -- and F-89 makes it the owner's switch, not the client's."""
    requests, _player, seen = make(allowed=False)
    with pytest.raises(EchoActError) as caught:
        requests.request("job_1")
    assert caught.value.code is Code.PLAYBACK_NOT_ALLOWED
    assert caught.value.retry_after_s is None
    assert seen == []


def test_the_owner_can_turn_it_on_without_restarting_anything() -> None:
    requests, _player, seen = make(allowed=False)
    requests.apply_settings(Settings(external_play=True))
    requests.request("job_1")
    assert len(seen) == 1


def test_one_external_request_gives_way_to_the_next() -> None:
    """Two "say this" calls in a row must both be heard in turn, so audio the
    gate itself started is not the owner's listening."""
    requests, player, seen = make()
    requests.request("job_1")
    requests.took("job_1")
    player.state = PlayerState.PLAYING

    requests.request("job_2")

    assert [r.job_id for r in seen] == ["job_1", "job_2"]


def test_the_owner_taking_the_speaker_back_closes_it_to_clients() -> None:
    requests, player, _seen = make()
    requests.request("job_1")
    requests.took("job_1")
    # The window stopped external playback because the owner pressed play.
    requests.took(None)
    player.state = PlayerState.PLAYING

    with pytest.raises(EchoActError) as caught:
        requests.request("job_2")
    assert caught.value.code is Code.PLAYBACK_BUSY


def test_a_listener_that_fails_does_not_fail_the_request() -> None:
    """The client was told its request was accepted; a broken listener is the
    app's problem to log, not the client's to retry."""
    requests, _player, seen = make()

    def boom(_request: PlayRequest) -> None:
        raise RuntimeError("no")

    requests.listen(boom)
    requests.request("job_1")
    assert len(seen) == 1


def test_unsubscribing_stops_the_news() -> None:
    requests, _player, seen = make()
    off = requests.listen(seen.append)
    off()
    requests.request("job_1")
    assert len(seen) == 1  # the fixture's own listener, not the second one
