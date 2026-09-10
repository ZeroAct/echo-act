"""Issuing, storing, and checking the credentials the local REST service uses.

F-71 fixes the hard part: the credential is shown once at creation and the app
keeps only a verifier from which it cannot be recovered.  So nothing here ever
writes a token to disk, to a log, or to a backup -- ``Verifier`` holds a salt
and an scrypt digest, and that is the whole of what survives ``issue``.

N-31 decides the rest of the shape.  Because the service listens from first
launch, this module fails closed: an empty store authenticates nothing, there
is no default or well-known credential anywhere in a distribution, and the
owner credential exists only because ``ensure_owner_credential`` *mints* one on
first launch -- a minted secret is unique to the installation, a shipped one
would be public the day the installer is.

The module is pure policy.  It imports no web framework, so F-71's client
management screen in the GUI evaluates exactly the same rules the service does
and cannot drift from them (N-24).

What this module deliberately does not do: cancel jobs.  5.3 requires a
revocation to cancel that client's in-progress jobs, but the job engine owns
that.  ``revoke`` and ``set_capabilities`` return the affected client id so the
caller can do it, which keeps this module free of a dependency on the engine
and keeps the GUI able to preview a revocation's consequences.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from enum import StrEnum
from hashlib import scrypt
from pathlib import Path
from typing import Any, Final

from ..domain import Capability
from ..errors import Code, EchoActError
from ..paths import data_dir
from ..policy import (
    CREDENTIAL_DAYS_DEFAULT,
    CREDENTIAL_DAYS_MAX,
    CREDENTIAL_DAYS_MIN,
    CREDENTIAL_EXPIRY_WARNING_DAYS,
)
from ..util.ids import client_id as mint_client_id
from ..util.ids import now as wall_now

DAY_S: Final = 86_400.0

#: Token shape: ``eak_<reference>_<44 base64url characters>``.
#:
#: The reference is readable so a person holding two tokens can tell them
#: apart, and it is also the store's lookup key.  It never contains ``_``,
#: which is what makes ``split("_", 2)`` unambiguous even though base64url's
#: own alphabet does contain ``_``.
TOKEN_PREFIX: Final = "eak"
TOKEN_SECRET_BYTES: Final = 33  # 264 bits -> exactly 44 base64url characters
TOKEN_SECRET_CHARS: Final = 44
_REF_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SECRET_RE: Final = re.compile(r"^[A-Za-z0-9_-]{32,}$")

# scrypt cost.  Measured at 35 ms on the development machine (16 MiB), which is
# 3.5% of N-22's one-second p95 and is paid on every authenticated request.  A
# heavier setting is not obviously right: the secret is 264 bits of CSPRNG
# output, so the KDF is not protecting a guessable password.  It only slows an
# attacker who has already read the verifier file, and N-19 states outright
# that this app does not defend against an attacker inside the same OS account.
# The cost is here because a verifier should not be a bare digest, not because
# it is the load-bearing defence.
SCRYPT_N: Final = 1 << 14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SCRYPT_DKLEN: Final = 32
_SCRYPT_MAXMEM: Final = 64 * 1024 * 1024

CREDENTIALS_FILENAME: Final = "credentials.json"
_SCHEMA_VERSION: Final = 1

#: 4.2 excludes credentials from backups.  A backup that walks the data
#: directory must skip this name; there is nothing in the file a restore could
#: usefully carry anyway, since the tokens themselves are unrecoverable.
BACKUP_EXCLUDED_FILENAMES: Final = frozenset({CREDENTIALS_FILENAME})


def credentials_path() -> Path:
    """Where the verifier store lives.

    ``echoact.paths`` has no entry for this file, so it is derived from
    ``data_dir()`` here and the ``ECHOACT_DATA_DIR`` override still applies.
    """
    return data_dir() / CREDENTIALS_FILENAME


class CredentialStatus(StrEnum):
    """What F-71's management screen shows next to a client."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


@dataclass(frozen=True, slots=True)
class Verifier:
    """What replaces the credential in storage (F-71, N-17).

    scrypt over the *whole* token with a per-credential salt.  Hashing the
    reference along with the secret means a verifier lifted from one entry
    cannot be pasted onto another and still match.
    """

    salt: bytes
    digest: bytes
    n: int = SCRYPT_N
    r: int = SCRYPT_R
    p: int = SCRYPT_P
    dklen: int = SCRYPT_DKLEN

    @classmethod
    def for_token(cls, token: str, *, salt: bytes | None = None) -> Verifier:
        salt = salt if salt is not None else secrets.token_bytes(16)
        return cls(salt=salt, digest=cls(salt=salt, digest=b"")._derive(token))

    def _derive(self, token: str) -> bytes:
        return scrypt(
            token.encode("utf-8"),
            salt=self.salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.dklen,
            maxmem=_SCRYPT_MAXMEM,
        )

    def matches(self, token: str) -> bool:
        """Constant-time comparison: a byte-by-byte one leaks the digest."""
        return hmac.compare_digest(self._derive(token), self.digest)

    def to_record(self) -> dict[str, Any]:
        return {
            "algorithm": "scrypt",
            "salt": _b64e(self.salt),
            "digest": _b64e(self.digest),
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "dklen": self.dklen,
        }

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> Verifier:
        if d.get("algorithm") != "scrypt":
            raise ValueError(f"unsupported verifier algorithm {d.get('algorithm')!r}")
        n, r, p = int(d["n"]), int(d["r"]), int(d["p"])
        # The file sits in the user's own data directory, but a corrupted cost
        # still has to be refused rather than handed to scrypt, where a large
        # n turns every later authentication into a memory error.
        if n < 1024 or r < 1 or p < 1 or 128 * r * n > _SCRYPT_MAXMEM:
            raise ValueError("verifier cost is outside the supported range")
        return cls(
            salt=_b64d(d["salt"]),
            digest=_b64d(d["digest"]),
            n=n,
            r=r,
            p=p,
            dklen=int(d["dklen"]),
        )

    def __repr__(self) -> str:  # never render key material, not even in a traceback
        return f"Verifier(algorithm='scrypt', n={self.n}, r={self.r}, p={self.p})"


@dataclass(slots=True)
class Credential:
    """One integration client: everything F-71 displays, plus a verifier.

    Mutable because it has a lifecycle -- capabilities change, access is
    recorded, revocation is stamped -- and because the store hands out the live
    object, so a revocation reaches a request that is already in flight (F-71:
    revocation applies immediately, including to result retrieval).
    """

    ref: str
    client_id: str
    name: str
    capabilities: frozenset[Capability]
    created_at: float
    expires_at: float
    verifier: Verifier
    last_access_at: float | None = None
    revoked_at: float | None = None

    @property
    def is_owner(self) -> bool:
        return Capability.OWNER in self.capabilities

    @property
    def effective_capabilities(self) -> frozenset[Capability]:
        """F-61 grants the three separately; owner implies all of them."""
        if self.is_owner:
            return frozenset(Capability)
        return self.capabilities

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.effective_capabilities

    def status(self, now: float | None = None) -> CredentialStatus:
        if self.revoked_at is not None:
            return CredentialStatus.REVOKED
        if (now if now is not None else wall_now()) >= self.expires_at:
            return CredentialStatus.EXPIRED
        return CredentialStatus.ACTIVE

    def is_usable(self, now: float | None = None) -> bool:
        return self.status(now) is CredentialStatus.ACTIVE

    def seconds_until_expiry(self, now: float | None = None) -> float:
        return self.expires_at - (now if now is not None else wall_now())

    def days_until_expiry(self, now: float | None = None) -> float:
        return self.seconds_until_expiry(now) / DAY_S

    def expiry_warning(self, now: float | None = None) -> bool:
        """4.1: a notice appears in the app 7 days before expiry.

        A revoked or already-expired credential is not "about to expire"; that
        is a different notice, so this stays false for both.
        """
        if self.status(now) is not CredentialStatus.ACTIVE:
            return False
        return self.seconds_until_expiry(now) <= CREDENTIAL_EXPIRY_WARNING_DAYS * DAY_S

    def to_public_dict(self, now: float | None = None) -> dict[str, Any]:
        """The projection F-71's screen, F-72's export, and any log may see.

        It omits the verifier entirely -- salt and digest included.  Neither is
        the credential, but 4.2 keeps credentials out of backups and logs, and
        the surest way to honour that is for the only general-purpose
        serialisation of this type to contain no key material at all.
        """
        return {
            "ref": self.ref,
            "client_id": self.client_id,
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "effective_capabilities": sorted(c.value for c in self.effective_capabilities),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_access_at": self.last_access_at,
            "revoked_at": self.revoked_at,
            "status": self.status(now).value,
            "expiry_warning": self.expiry_warning(now),
        }

    def to_record(self) -> dict[str, Any]:
        """The on-disk form: the public projection plus the verifier.

        The token is not derivable from anything in here -- scrypt is one-way
        and a 264-bit secret is not searchable -- which is how F-71's "stores
        only a verifier from which the credential cannot be recovered" becomes
        a property of the file rather than a promise about the code.

        ``status`` and ``expiry_warning`` ride along because the public
        projection carries them; they are derived from the timestamps and
        :meth:`from_record` ignores them, so a stale value in the file changes
        nothing.
        """
        return {**self.to_public_dict(), "verifier": self.verifier.to_record()}

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> Credential:
        return cls(
            ref=str(d["ref"]),
            client_id=str(d["client_id"]),
            name=str(d["name"]),
            capabilities=frozenset(Capability(c) for c in d["capabilities"]),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
            verifier=Verifier.from_record(d["verifier"]),
            last_access_at=None if d.get("last_access_at") is None else float(d["last_access_at"]),
            revoked_at=None if d.get("revoked_at") is None else float(d["revoked_at"]),
        )

    def __repr__(self) -> str:
        return (
            f"Credential(ref={self.ref!r}, client_id={self.client_id!r}, "
            f"capabilities={sorted(c.value for c in self.capabilities)})"
        )


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """The one moment the token exists (F-71: shown once at creation).

    Hand it to the owner, let them copy it, drop it.  Nothing else in the app
    may keep it, so ``__repr__`` refuses to render it: an accidental
    ``log.info("%s", issued)`` would otherwise put a live credential into a
    file N-20 says must never contain one.
    """

    credential: Credential
    token: str

    @property
    def ref(self) -> str:
        return self.credential.ref

    @property
    def client_id(self) -> str:
        return self.credential.client_id

    def __repr__(self) -> str:
        return f"IssuedCredential(ref={self.credential.ref!r}, token=<shown once>)"


@dataclass(frozen=True, slots=True)
class PermissionChange:
    """What a capability edit implies for jobs already running (5.3, F-52)."""

    client_id: str
    before: frozenset[Capability]
    after: frozenset[Capability]

    @property
    def narrowed(self) -> bool:
        return bool(self.before - self.after)

    @property
    def cancels_jobs(self) -> bool:
        """5.3 cancels a client's in-progress jobs when its permission is
        revoked.  Losing GENERATE (or OWNER, which implied it) is that case.
        Gaining a capability is not, and losing only a read capability leaves a
        running job legitimate -- its result simply becomes unreadable."""
        lost = self.before - self.after
        return Capability.GENERATE in lost or Capability.OWNER in lost


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24].strip("-")
    return slug or "client"


def _validate_days(days: int) -> int:
    if not isinstance(days, int) or days < CREDENTIAL_DAYS_MIN or days > CREDENTIAL_DAYS_MAX:
        raise EchoActError(
            Code.INTERNAL,
            f"Credential lifetime must be between {CREDENTIAL_DAYS_MIN} and "
            f"{CREDENTIAL_DAYS_MAX} days.",
            detail={"days": days, "min": CREDENTIAL_DAYS_MIN, "max": CREDENTIAL_DAYS_MAX},
        )
    return days


def authorise(
    credential: Credential,
    job_owner_id: str | None,
    capability: Capability,
    *,
    now: float | None = None,
) -> None:
    """F-50, F-61, and N-19 in one check.  Returns None, or raises.

    Both halves are checked, always.  N-19 says knowing a job id must not by
    itself grant access, so holding the capability is not enough: the object
    has to belong to the caller.  Pass ``job_owner_id=None`` for an operation
    that is not about an existing object -- creating a job, listing one's own
    history -- and never as a shortcut for "the owner is unknown".

    The two failures answer with different codes on purpose.  A missing
    capability is FORBIDDEN: the caller's own credential is the problem and
    saying so helps them fix it.  A caller asking about *someone else's* job
    gets NOT_FOUND rather than FORBIDDEN, because FORBIDDEN would confirm the
    id exists and turn the endpoint into an oracle for job ids, which is the
    thing N-19 is about.  Capability is checked first so that answer never
    depends on whether the id was real.
    """
    status = credential.status(now)
    if status is CredentialStatus.REVOKED:
        raise EchoActError(Code.CREDENTIAL_REVOKED, detail={"ref": credential.ref})
    if status is CredentialStatus.EXPIRED:
        # F-71: expiry blocks result retrieval, not only new access, so this is
        # re-evaluated per operation rather than once at authentication.
        raise EchoActError(Code.CREDENTIAL_EXPIRED, detail={"ref": credential.ref})
    if not credential.has_capability(capability):
        raise EchoActError(Code.FORBIDDEN, detail={"capability": capability.value})
    if job_owner_id is None or credential.is_owner:
        # F-50: the GUI owner reviews and cancels all jobs.
        return
    if job_owner_id != credential.client_id:
        raise EchoActError(Code.NOT_FOUND)


def can_access(
    credential: Credential,
    job_owner_id: str | None,
    capability: Capability,
    *,
    now: float | None = None,
) -> bool:
    """The same rule as :func:`authorise`, as a predicate.

    F-71's client screen and F-69's job list need to grey an action out rather
    than provoke an error to discover it is unavailable.
    """
    try:
        authorise(credential, job_owner_id, capability, now=now)
    except EchoActError:
        return False
    return True


class CredentialStore:
    """The set of issued credentials, persisted as verifiers.

    Thread-safe: the REST service authenticates from several threads while the
    GUI edits permissions on its own.  The lock is deliberately *not* held
    across the scrypt derivation -- that would serialise every request behind a
    35 ms hash -- so ``authenticate`` copies the verifier out, derives outside
    the lock, then re-reads the entry to decide on state that may have changed
    meanwhile.  A revocation therefore wins a race against an in-flight
    authentication, which is the direction F-71 wants it decided.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else credentials_path()
        self._lock = threading.RLock()
        self._by_ref: dict[str, Credential] = {}
        # An unknown reference must cost what a known one costs.  Without this
        # decoy, response time answers "does this client exist?" for free.
        self._decoy = Verifier.for_token(
            f"{TOKEN_PREFIX}_decoy_{secrets.token_urlsafe(TOKEN_SECRET_BYTES)}"
        )

    # -- construction ---------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> CredentialStore:
        """Read the store, or return an empty one on first launch.

        A file that exists but cannot be parsed is an error, not an empty
        store.  Treating it as empty would mint a fresh owner credential over
        the top on the next call and silently revoke every client the user had
        registered; F-79's answer -- report the integration unavailable and
        keep the rest of the app working -- is the better failure.
        """
        store = cls(path)
        try:
            raw = store._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return store
        except OSError as exc:
            raise EchoActError(
                Code.INTERNAL, "The credential store could not be read.", cause=exc
            ) from exc
        try:
            doc = json.loads(raw)
            if int(doc.get("schema", 0)) != _SCHEMA_VERSION:
                raise ValueError(f"unsupported credential schema {doc.get('schema')!r}")
            for record in doc["credentials"]:
                cred = Credential.from_record(record)
                store._by_ref[cred.ref] = cred
        except (ValueError, KeyError, TypeError) as exc:
            raise EchoActError(
                Code.INTERNAL, "The credential store is damaged.", cause=exc
            ) from exc
        return store

    # -- reading --------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_ref)

    def list(self) -> list[Credential]:
        """Every client, newest first -- F-71's management screen."""
        with self._lock:
            return sorted(self._by_ref.values(), key=lambda c: c.created_at, reverse=True)

    def get(self, ref: str) -> Credential:
        with self._lock:
            cred = self._by_ref.get(ref)
        if cred is None:
            raise EchoActError(Code.NOT_FOUND, detail={"ref": ref})
        return cred

    def find_by_client(self, client_id: str) -> Credential | None:
        with self._lock:
            for cred in self._by_ref.values():
                if cred.client_id == client_id:
                    return cred
        return None

    def owner_credential(self) -> Credential | None:
        with self._lock:
            for cred in self._by_ref.values():
                if cred.is_owner and cred.revoked_at is None:
                    return cred
        return None

    def expiring_soon(self, *, now: float | None = None) -> list[Credential]:
        """4.1's seven-day notice, for whichever surface draws it."""
        at = now if now is not None else wall_now()
        return [c for c in self.list() if c.expiry_warning(at)]

    def export_public(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Credential state with no key material, for the GUI and F-72.

        There is no ``export_private`` counterpart.  A backup gets nothing at
        all from this module (4.2); a diagnostic export gets this.
        """
        at = now if now is not None else wall_now()
        return [c.to_public_dict(at) for c in self.list()]

    # -- issuing --------------------------------------------------------

    def issue(
        self,
        name: str,
        capabilities: frozenset[Capability] | set[Capability] | tuple[Capability, ...],
        *,
        days: int = CREDENTIAL_DAYS_DEFAULT,
        now: float | None = None,
    ) -> IssuedCredential:
        """Mint a credential for a new client.  The token is returned once.

        4.1 sets the default lifetime at 90 days and lets the owner choose 1 to
        365.  Every call mints its own client id, its own salt, and its own
        secret, so N-31's "no credential is shared between clients" is a
        property of the minting rather than of anyone's discipline.
        """
        _validate_days(days)
        at = now if now is not None else wall_now()
        caps = frozenset(capabilities)
        with self._lock:
            ref = self._unique_ref(name)
            token = f"{TOKEN_PREFIX}_{ref}_{secrets.token_urlsafe(TOKEN_SECRET_BYTES)}"
            cred = Credential(
                ref=ref,
                client_id=mint_client_id(),
                name=name,
                capabilities=caps,
                created_at=at,
                expires_at=at + days * DAY_S,
                verifier=Verifier.for_token(token),
            )
            self._by_ref[ref] = cred
            self._save_locked()
        return IssuedCredential(credential=cred, token=token)

    def ensure_owner_credential(
        self,
        *,
        name: str = "Owner",
        days: int = CREDENTIAL_DAYS_DEFAULT,
        now: float | None = None,
    ) -> IssuedCredential | None:
        """F-71: create the owner credential on first launch, or do nothing.

        Returns the issued credential the first time so the caller can show it
        for copying, and ``None`` afterwards -- there is no way to ask for it
        again, because nothing kept it.  A lost owner credential is reissued
        (:meth:`reissue`), the same path every other client uses.

        This is precisely why a distribution contains no credential at all
        (N-31): the secret comes into existence on the user's own machine, at
        first launch, and differs between every installation.
        """
        with self._lock:
            if self.owner_credential() is not None:
                return None
            return self.issue(name, frozenset({Capability.OWNER}), days=days, now=now)

    def reissue(
        self, ref: str, *, days: int | None = None, now: float | None = None
    ) -> IssuedCredential:
        """Replace a lost or expired credential for the same client (F-71, 4.1).

        The client id and the capabilities are kept, so jobs and history the
        client already owns stay reachable -- 4.1 says access is possible again
        with reissued credentials for the same client.  The old verifier is
        replaced, so the previous token stops working the moment this returns.

        A revoked credential is not reissued.  Revocation is a deliberate act
        that also cancelled that client's jobs (5.3); quietly undoing it here
        would make the owner's decision reversible by accident.  Issue a new
        client instead.
        """
        at = now if now is not None else wall_now()
        with self._lock:
            cred = self.get(ref)
            if cred.revoked_at is not None:
                raise EchoActError(Code.CREDENTIAL_REVOKED, detail={"ref": ref})
            lifetime = _validate_days(days) if days is not None else CREDENTIAL_DAYS_DEFAULT
            token = f"{TOKEN_PREFIX}_{ref}_{secrets.token_urlsafe(TOKEN_SECRET_BYTES)}"
            cred.verifier = Verifier.for_token(token)
            cred.expires_at = at + lifetime * DAY_S
            self._save_locked()
        return IssuedCredential(credential=cred, token=token)

    # -- changing -------------------------------------------------------

    def set_capabilities(
        self, ref: str, capabilities: frozenset[Capability] | set[Capability]
    ) -> PermissionChange:
        """F-71's permission change, effective immediately.

        Immediately means no reissue and no restart: authentication and
        :meth:`authorise` read the live entry, so the next call already sees
        the new set.  The returned change says whether the caller must now
        cancel that client's jobs (5.3); this module never cancels anything.
        """
        after = frozenset(capabilities)
        with self._lock:
            cred = self.get(ref)
            before = cred.capabilities
            cred.capabilities = after
            self._save_locked()
        return PermissionChange(client_id=cred.client_id, before=before, after=after)

    def revoke(self, ref: str, *, now: float | None = None) -> str:
        """Revoke a credential and report whose jobs must be cancelled.

        Returns the client id.  5.3 requires that client's in-progress jobs to
        be cancelled too, and F-52 requires the same when an integration is
        switched off, but cancelling belongs to the job engine -- this returns
        the one fact the engine needs.  Revoking twice returns the same id and
        changes nothing further (F-49's "repeated cancellation adds no side
        effects", applied to permissions).
        """
        at = now if now is not None else wall_now()
        with self._lock:
            cred = self.get(ref)
            if cred.revoked_at is None:
                cred.revoked_at = at
                self._save_locked()
            return cred.client_id

    def forget(self, ref: str) -> str:
        """Delete the entry outright, for F-76's "revoke integration permissions".

        Returns the client id for the same reason :meth:`revoke` does.  Prefer
        :meth:`revoke` wherever the owner should still see the client listed
        with a revoked state.
        """
        with self._lock:
            cred = self.get(ref)
            del self._by_ref[ref]
            self._save_locked()
            return cred.client_id

    # -- authenticating -------------------------------------------------

    def authenticate(self, token: str, *, now: float | None = None) -> Credential:
        """Resolve a presented token, or raise.

        Failures are deliberately uninformative: an unparsable token, an
        unknown reference, and a wrong secret are all UNAUTHENTICATED and all
        cost one scrypt derivation, so neither the answer nor the timing says
        which clients exist.  Only once the secret matches does the caller
        learn that the credential is expired or revoked -- learning that is
        harmless, since it required holding the credential.
        """
        at = now if now is not None else wall_now()
        parsed = _parse_token(token)
        with self._lock:
            cred = self._by_ref.get(parsed[0]) if parsed is not None else None
            verifier = cred.verifier if cred is not None else self._decoy

        matched = verifier.matches(token) if isinstance(token, str) else False
        if cred is None or parsed is None or not matched:
            raise EchoActError(Code.UNAUTHENTICATED)

        with self._lock:
            # Re-read under the lock: a revocation or a reissue may have landed
            # while the derivation ran, and it has to win.
            current = self._by_ref.get(cred.ref)
            if current is None or current.verifier is not verifier:
                raise EchoActError(Code.UNAUTHENTICATED)
            status = current.status(at)
            if status is CredentialStatus.REVOKED:
                raise EchoActError(Code.CREDENTIAL_REVOKED, detail={"ref": current.ref})
            if status is CredentialStatus.EXPIRED:
                raise EchoActError(Code.CREDENTIAL_EXPIRED, detail={"ref": current.ref})
            current.last_access_at = at
            self._save_locked()
            return current

    def authorise(
        self,
        credential: Credential,
        job_owner_id: str | None,
        capability: Capability,
        *,
        now: float | None = None,
    ) -> None:
        """:func:`authorise`, re-resolved against the store's live entry.

        A request holds a ``Credential`` from the moment it authenticates;
        between then and reading a result the owner may have revoked or
        narrowed it, and F-71 says that applies to result retrieval
        immediately.  Looking the reference up again is what makes
        "immediately" true even for a request that has been running a while,
        and it also covers an entry deleted outright by :meth:`forget`.
        """
        with self._lock:
            current = self._by_ref.get(credential.ref)
        if current is None:
            raise EchoActError(Code.CREDENTIAL_REVOKED, detail={"ref": credential.ref})
        authorise(current, job_owner_id, capability, now=now)

    # -- persistence ----------------------------------------------------

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        doc = {
            "schema": _SCHEMA_VERSION,
            "credentials": [c.to_record() for c in self._by_ref.values()],
        }
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                # Best effort: POSIX modes do not carry on every filesystem, and
                # N-19 already places the same-account attacker out of scope.
                pass
            os.replace(tmp, self._path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise EchoActError(
                Code.INTERNAL, "The credential store could not be written.", cause=exc
            ) from exc

    def _unique_ref(self, name: str) -> str:
        base = _slug(name)
        while True:
            ref = f"{base}-{secrets.token_hex(3)}"
            if ref not in self._by_ref:
                return ref


def _parse_token(token: str) -> tuple[str, str] | None:
    """Split ``eak_<ref>_<secret>``, or return None if it is not one of ours.

    Split from the left with a limit of two: the secret is base64url and may
    contain ``_`` itself, so anything that splits from the right, or on every
    separator, mis-parses a legitimate token roughly half the time.
    """
    if not isinstance(token, str) or len(token) > 512:
        return None
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    ref, secret = parts[1], parts[2]
    if _REF_RE.match(ref) is None or _SECRET_RE.match(secret) is None:
        return None
    return ref, secret


__all__ = [
    "BACKUP_EXCLUDED_FILENAMES",
    "CREDENTIALS_FILENAME",
    "TOKEN_PREFIX",
    "TOKEN_SECRET_CHARS",
    "Credential",
    "CredentialStatus",
    "CredentialStore",
    "IssuedCredential",
    "PermissionChange",
    "Verifier",
    "authorise",
    "can_access",
    "credentials_path",
]
