"""F-71, F-61, F-50, N-17, N-19, N-31: credentials and permission boundaries."""

from __future__ import annotations

import json
import threading

import pytest

from echoact import paths
from echoact.domain import Capability
from echoact.errors import Code, EchoActError
from echoact.policy import (
    CREDENTIAL_DAYS_DEFAULT,
    CREDENTIAL_DAYS_MAX,
    CREDENTIAL_DAYS_MIN,
    CREDENTIAL_EXPIRY_WARNING_DAYS,
)
from echoact.security.credentials import (
    DAY_S,
    TOKEN_SECRET_CHARS,
    CredentialStatus,
    CredentialStore,
    authorise,
    can_access,
    credentials_path,
)

T0 = 1_800_000_000.0  # a fixed wall clock, so expiry arithmetic is exact


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Never touch the real user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path))
    paths.data_dir.cache_clear()
    yield tmp_path
    paths.data_dir.cache_clear()


@pytest.fixture
def store():
    return CredentialStore.load()


def _client(store, caps=(Capability.GENERATE,), *, name="Tool", days=CREDENTIAL_DAYS_DEFAULT):
    return store.issue(name, frozenset(caps), days=days, now=T0)


# -- N-31: fail closed, nothing shipped, nothing shared --------------------


def test_a_fresh_installation_authenticates_nothing(store):
    assert len(store) == 0
    for attempt in ("", "eak_", "eak_owner_" + "A" * 44, "Bearer hunter2"):
        with pytest.raises(EchoActError) as caught:
            store.authenticate(attempt, now=T0)
        assert caught.value.code is Code.UNAUTHENTICATED


def test_the_owner_credential_is_minted_on_first_launch_and_never_shipped(tmp_path):
    first = CredentialStore.load()
    issued = first.ensure_owner_credential(now=T0)
    assert issued is not None
    assert first.ensure_owner_credential(now=T0) is None, "minted once, not on every launch"

    other_machine = CredentialStore.load(tmp_path / "elsewhere.json")
    other = other_machine.ensure_owner_credential(now=T0)
    assert other is not None
    assert other.token != issued.token, "a shipped or derived secret would collide here"
    with pytest.raises(EchoActError):
        other_machine.authenticate(issued.token, now=T0)


def test_two_clients_share_no_secret_and_no_verifier(store):
    a = _client(store, name="Alpha")
    b = _client(store, name="Beta")
    assert a.token != b.token
    assert a.credential.client_id != b.credential.client_id
    assert a.credential.verifier.salt != b.credential.verifier.salt
    assert a.credential.verifier.digest != b.credential.verifier.digest
    with pytest.raises(EchoActError):
        store.authenticate(f"eak_{b.ref}_{a.token.split('_', 2)[2]}", now=T0)


# -- F-71: shown once, stored only as a verifier ---------------------------


def test_the_token_shape_is_a_readable_prefix_and_high_entropy_random(store):
    issued = _client(store, name="Claude Desktop")
    prefix, ref, secret = issued.token.split("_", 2)
    assert prefix == "eak"
    assert ref.startswith("claude-desktop-")
    assert len(secret) == TOKEN_SECRET_CHARS
    assert set(secret) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_the_stored_file_contains_no_part_of_the_credential(store):
    issued = _client(store, name="Alpha")
    raw = credentials_path().read_text(encoding="utf-8")
    secret = issued.token.split("_", 2)[2]
    assert issued.token not in raw
    assert secret not in raw
    record = json.loads(raw)["credentials"][0]
    assert set(record["verifier"]) == {"algorithm", "salt", "digest", "n", "r", "p", "dklen"}
    assert store.authenticate(issued.token, now=T0).ref == issued.ref


def test_the_token_is_not_rendered_by_repr_or_the_public_projection(store):
    issued = _client(store, name="Alpha")
    assert issued.token not in repr(issued)
    assert issued.token not in repr(issued.credential)
    assert issued.token not in repr(issued.credential.verifier)
    public = store.export_public(now=T0)[0]
    assert "verifier" not in public
    assert issued.token not in json.dumps(public)
    assert set(public) >= {"name", "capabilities", "last_access_at", "expires_at", "status"}


def test_a_wrong_secret_for_a_real_reference_is_refused(store):
    issued = _client(store)
    ref = issued.ref
    with pytest.raises(EchoActError) as caught:
        store.authenticate(f"eak_{ref}_{'A' * TOKEN_SECRET_CHARS}", now=T0)
    assert caught.value.code is Code.UNAUTHENTICATED


def test_authentication_records_the_last_access_time_f71_displays(store):
    issued = _client(store)
    assert issued.credential.last_access_at is None
    store.authenticate(issued.token, now=T0 + 5.0)
    assert store.get(issued.ref).last_access_at == T0 + 5.0
    assert CredentialStore.load().get(issued.ref).last_access_at == T0 + 5.0


# -- 4.1: lifetime, warning, reissue --------------------------------------


def test_the_default_lifetime_is_ninety_days(store):
    issued = _client(store)
    assert issued.credential.expires_at == T0 + CREDENTIAL_DAYS_DEFAULT * DAY_S


@pytest.mark.parametrize("days", [CREDENTIAL_DAYS_MIN, 30, CREDENTIAL_DAYS_MAX])
def test_the_owner_may_choose_one_to_three_hundred_and_sixty_five_days(store, days):
    issued = _client(store, days=days)
    assert issued.credential.expires_at == T0 + days * DAY_S


@pytest.mark.parametrize("days", [0, -1, CREDENTIAL_DAYS_MAX + 1, 10_000])
def test_a_lifetime_outside_the_allowed_range_is_refused(store, days):
    with pytest.raises(EchoActError):
        _client(store, days=days)


def test_a_warning_appears_seven_days_before_expiry(store):
    issued = _client(store, days=90)
    cred = issued.credential
    quiet = T0 + (90 - CREDENTIAL_EXPIRY_WARNING_DAYS - 1) * DAY_S
    warning = T0 + (90 - CREDENTIAL_EXPIRY_WARNING_DAYS) * DAY_S + 1.0
    assert cred.expiry_warning(quiet) is False
    assert store.expiring_soon(now=quiet) == []
    assert cred.expiry_warning(warning) is True
    assert store.expiring_soon(now=warning) == [cred]
    # Once it has actually expired it is no longer a "expires soon" notice.
    assert cred.expiry_warning(T0 + 91 * DAY_S) is False


def test_an_expired_credential_blocks_new_access_and_result_retrieval(store):
    issued = _client(store, caps=(Capability.GENERATE, Capability.READ_RESULTS))
    later = T0 + (CREDENTIAL_DAYS_DEFAULT + 1) * DAY_S

    with pytest.raises(EchoActError) as caught:
        store.authenticate(issued.token, now=later)
    assert caught.value.code is Code.CREDENTIAL_EXPIRED

    # F-71 applies expiry to result retrieval as well, for a credential the
    # request is already holding.
    with pytest.raises(EchoActError) as caught:
        store.authorise(
            issued.credential, issued.client_id, Capability.READ_RESULTS, now=later
        )
    assert caught.value.code is Code.CREDENTIAL_EXPIRED


def test_reissuing_restores_access_for_the_same_client(store):
    issued = _client(store, caps=(Capability.READ_RESULTS,))
    later = T0 + (CREDENTIAL_DAYS_DEFAULT + 1) * DAY_S
    again = store.reissue(issued.ref, now=later)

    assert again.client_id == issued.client_id, "4.1: the same client, not a new one"
    assert again.credential.capabilities == frozenset({Capability.READ_RESULTS})
    assert again.token != issued.token
    assert store.authenticate(again.token, now=later).client_id == issued.client_id
    with pytest.raises(EchoActError) as caught:
        store.authenticate(issued.token, now=later)
    assert caught.value.code is Code.UNAUTHENTICATED


def test_a_revoked_credential_is_not_quietly_reissued(store):
    issued = _client(store)
    store.revoke(issued.ref, now=T0)
    with pytest.raises(EchoActError) as caught:
        store.reissue(issued.ref, now=T0)
    assert caught.value.code is Code.CREDENTIAL_REVOKED


# -- F-71 / 5.3: revocation ------------------------------------------------


def test_revocation_applies_immediately_and_names_the_client_to_cancel(store):
    issued = _client(store, caps=(Capability.GENERATE, Capability.READ_RESULTS))
    store.authenticate(issued.token, now=T0)

    client_id = store.revoke(issued.ref, now=T0 + 1.0)
    assert client_id == issued.client_id, "5.3: the caller cancels that client's jobs"

    with pytest.raises(EchoActError) as caught:
        store.authenticate(issued.token, now=T0 + 2.0)
    assert caught.value.code is Code.CREDENTIAL_REVOKED

    # A request already holding the credential loses result access too.
    with pytest.raises(EchoActError) as caught:
        store.authorise(
            issued.credential, issued.client_id, Capability.READ_RESULTS, now=T0 + 2.0
        )
    assert caught.value.code is Code.CREDENTIAL_REVOKED
    assert store.get(issued.ref).status(T0 + 2.0) is CredentialStatus.REVOKED


def test_revoking_twice_changes_nothing_further(store):
    issued = _client(store)
    first = store.revoke(issued.ref, now=T0 + 1.0)
    second = store.revoke(issued.ref, now=T0 + 9.0)
    assert first == second
    assert store.get(issued.ref).revoked_at == T0 + 1.0


def test_deleting_a_client_outright_also_blocks_a_held_credential(store):
    issued = _client(store, caps=(Capability.READ_RESULTS,))
    assert store.forget(issued.ref) == issued.client_id
    with pytest.raises(EchoActError) as caught:
        store.authorise(
            issued.credential, issued.client_id, Capability.READ_RESULTS, now=T0
        )
    assert caught.value.code is Code.CREDENTIAL_REVOKED


def test_narrowing_capabilities_takes_effect_without_a_reissue(store):
    issued = _client(store, caps=(Capability.GENERATE, Capability.READ_RESULTS))
    change = store.set_capabilities(issued.ref, frozenset({Capability.READ_RESULTS}))

    assert change.client_id == issued.client_id
    assert change.narrowed is True
    assert change.cancels_jobs is True, "5.3 cancels in-progress jobs when generate is revoked"
    live = store.authenticate(issued.token, now=T0)
    assert live.has_capability(Capability.GENERATE) is False
    with pytest.raises(EchoActError) as caught:
        store.authorise(issued.credential, issued.client_id, Capability.GENERATE, now=T0)
    assert caught.value.code is Code.FORBIDDEN


def test_granting_a_capability_does_not_cancel_anything(store):
    issued = _client(store, caps=(Capability.GENERATE,))
    change = store.set_capabilities(
        issued.ref, frozenset({Capability.GENERATE, Capability.READ_HISTORY})
    )
    assert change.narrowed is False
    assert change.cancels_jobs is False


# -- F-61 / F-50 / N-19: what a credential authorises ----------------------


def test_the_three_capabilities_are_granted_separately(store):
    issued = _client(store, caps=(Capability.GENERATE,))
    cred, owner_id = issued.credential, issued.client_id

    authorise(cred, owner_id, Capability.GENERATE, now=T0)
    for denied in (Capability.READ_RESULTS, Capability.READ_HISTORY):
        with pytest.raises(EchoActError) as caught:
            authorise(cred, owner_id, denied, now=T0)
        assert caught.value.code is Code.FORBIDDEN
        assert can_access(cred, owner_id, denied, now=T0) is False


def test_reading_history_is_separate_from_reading_results(store):
    issued = _client(store, caps=(Capability.READ_RESULTS,))
    authorise(issued.credential, issued.client_id, Capability.READ_RESULTS, now=T0)
    with pytest.raises(EchoActError) as caught:
        authorise(issued.credential, issued.client_id, Capability.READ_HISTORY, now=T0)
    assert caught.value.code is Code.FORBIDDEN


def test_the_owner_credential_implies_every_capability_and_sees_every_job(store):
    owner = store.ensure_owner_credential(now=T0)
    other = _client(store, name="Alpha")
    assert owner is not None
    for capability in (Capability.GENERATE, Capability.READ_RESULTS, Capability.READ_HISTORY):
        authorise(owner.credential, other.client_id, capability, now=T0)


def test_knowing_a_job_id_grants_nothing_by_itself(store):
    mine = _client(store, caps=(Capability.READ_RESULTS,), name="Mine")
    theirs = _client(store, caps=(Capability.READ_RESULTS,), name="Theirs")

    authorise(mine.credential, mine.client_id, Capability.READ_RESULTS, now=T0)
    with pytest.raises(EchoActError) as caught:
        authorise(mine.credential, theirs.client_id, Capability.READ_RESULTS, now=T0)
    # NOT_FOUND, not FORBIDDEN: FORBIDDEN would confirm the job exists.
    assert caught.value.code is Code.NOT_FOUND


def test_a_missing_capability_answers_the_same_for_a_real_and_an_unreal_job(store):
    mine = _client(store, caps=(Capability.GENERATE,), name="Mine")
    theirs = _client(store, caps=(Capability.GENERATE,), name="Theirs")

    codes = set()
    for owner_id in (mine.client_id, theirs.client_id, "cli_does_not_exist"):
        with pytest.raises(EchoActError) as caught:
            authorise(mine.credential, owner_id, Capability.READ_RESULTS, now=T0)
        codes.add(caught.value.code)
    assert codes == {Code.FORBIDDEN}, "the capability answer must not depend on the id"


def test_an_operation_without_an_object_only_needs_the_capability(store):
    issued = _client(store, caps=(Capability.GENERATE,))
    authorise(issued.credential, None, Capability.GENERATE, now=T0)
    assert can_access(issued.credential, None, Capability.GENERATE, now=T0) is True


def test_a_credential_with_no_capabilities_authorises_nothing(store):
    issued = store.issue("Disabled", frozenset(), now=T0)
    for capability in Capability:
        assert can_access(issued.credential, issued.client_id, capability, now=T0) is False


# -- persistence -----------------------------------------------------------


def test_credentials_survive_a_restart(store):
    issued = _client(store, caps=(Capability.GENERATE, Capability.READ_HISTORY), name="Alpha")
    reloaded = CredentialStore.load()
    cred = reloaded.authenticate(issued.token, now=T0 + 1.0)
    assert cred.client_id == issued.client_id
    assert cred.capabilities == frozenset({Capability.GENERATE, Capability.READ_HISTORY})


def test_a_damaged_store_is_reported_rather_than_replaced(store):
    _client(store, name="Alpha")
    credentials_path().write_text("{ not json", encoding="utf-8")
    with pytest.raises(EchoActError) as caught:
        CredentialStore.load()
    assert caught.value.code is Code.INTERNAL
    assert credentials_path().read_text(encoding="utf-8") == "{ not json"


def test_authentication_is_thread_safe(store):
    issued = _client(store)
    seen: list[str] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            seen.append(store.authenticate(issued.token, now=T0).client_id)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert failures == []
    assert seen == [issued.client_id] * 8


def test_a_revocation_during_an_in_flight_authentication_wins(store):
    issued = _client(store)
    revoked = threading.Event()
    result: dict[str, object] = {}

    original = type(issued.credential.verifier).matches

    def slow_matches(self, token: str) -> bool:
        outcome = original(self, token)
        revoked.wait(timeout=5)
        return outcome

    type(issued.credential.verifier).matches = slow_matches  # type: ignore[method-assign]
    try:
        def worker() -> None:
            try:
                store.authenticate(issued.token, now=T0)
                result["code"] = "allowed"
            except EchoActError as exc:
                result["code"] = exc.code

        thread = threading.Thread(target=worker)
        thread.start()
        store.revoke(issued.ref, now=T0)
        revoked.set()
        thread.join(timeout=30)
    finally:
        type(issued.credential.verifier).matches = original  # type: ignore[method-assign]

    assert result["code"] is Code.CREDENTIAL_REVOKED
