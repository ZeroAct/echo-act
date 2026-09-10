"""F-84's manifest: what a model *is*, as data that ships with the build.

F-84 requires every supported model to be defined by an entry recording its
source, pinned revision, per-file digests and total size, licence and usage
restrictions, native sample rate, supported languages, and the minimum
resource budget under which it is approved to run.  F-63 to F-65 resolve
download, verification, and repair against these entries, and a model whose
files do not match one is *corrupted*, never used.

Two consequences shape this module:

* The manifest is a Python literal in :mod:`echoact.models.catalog`, not a
  document fetched at start-up.  F-84 forbids a silent remote update, and a
  downloadable manifest is exactly that: it would let a changed digest
  redefine "sound" for weights already on disk.  Shipping it in the wheel
  makes a manifest change a release.
* Nothing here touches the disk or the network.  ``registry`` does that.  A
  manifest that could read a file would be tempted to trust what it read.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..domain import Budget, Gender, Language
from ..errors import Code, EchoActError

_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One file of a model, with the two facts F-84 needs to judge it.

    Size is checked before the digest because a truncated 256 MB download is
    the common failure and hashing it to learn that is a wasted second.  The
    digest is what actually decides: F-84 wants a *tampered* file reported as
    corrupt, and only the hash sees that.
    """

    relative_path: str
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        pure = PurePosixPath(self.relative_path)
        parts = pure.parts
        # A manifest path is joined onto the model cache root.  It ships with
        # the app, so a path escaping that root is a packaging mistake rather
        # than a runtime condition -- but it would be a directory traversal
        # if it ever got through, so it is refused here, not downstream.
        if not parts or pure.is_absolute():
            raise AssertionError(f"manifest path must be relative: {self.relative_path!r}")
        if any(p in ("..", ".", "") for p in parts) or ":" in self.relative_path:
            raise AssertionError(f"unsafe manifest path: {self.relative_path!r}")
        if len(self.sha256) != 64 or not _HEX.issuperset(self.sha256):
            raise AssertionError(f"sha256 must be 64 lowercase hex digits: {self.sha256!r}")
        if self.byte_size <= 0:
            raise AssertionError(f"non-positive size for {self.relative_path!r}")

    def path_under(self, root: Path) -> Path:
        """Where this file belongs beneath a model directory."""
        return root.joinpath(*PurePosixPath(self.relative_path).parts)


@dataclass(frozen=True, slots=True)
class VoiceEntry:
    """One selectable voice (F-06), described well enough for F-53.

    F-53 requires a description because a caller that is not a person has no
    basis for choosing between ``M1`` and ``M4`` otherwise.  ``characterised``
    records whether that description rests on anyone having listened: A.4
    keeps N-08 open, so every shipped description today states only what is
    verifiable -- gender, index, provenance -- and this flag is ``False``.  A
    later listening pass rewrites the text and flips the flag without a
    schema change, and a surface that wants to say "not yet reviewed" can.
    """

    voice_id: str
    gender: Gender
    display_name: str
    description: str
    characterised: bool = False

    def __post_init__(self) -> None:
        if not self.voice_id or not self.display_name or not self.description:
            raise AssertionError(f"incomplete voice entry: {self.voice_id!r}")


@dataclass(frozen=True, slots=True)
class LicenseTerms:
    """The licence as an obligation, not as a notice.

    N-11 makes this a release blocker rather than a footnote: where a model's
    licence obliges a distributor to bind its own users to the same use
    restrictions, the product's terms must carry them through.  So the
    restrictions are held here as text the app can show verbatim, and
    ``acceptance_required`` drives F-80's "accepted before first preparation"
    gate.  Filing the licence under Help would satisfy neither requirement.

    ``restrictions`` quotes the licence rather than paraphrasing it; a
    paraphrase is what a reader would hold the distributor to instead of the
    real term.
    """

    name: str
    #: One entry per restriction, in the licence's own order and wording.
    restrictions: tuple[str, ...]
    #: The clause that makes those restrictions the distributor's problem.
    pass_through_obligation: str
    #: N-11 / F-80: accepted before the model is first prepared.
    acceptance_required: bool
    #: Facts a reviewer must see that are not themselves use restrictions.
    notes: tuple[str, ...] = ()
    #: Path of the full licence inside the source repository, for F-80's
    #: "review the licence" affordance and for attribution.
    source_file: str = "LICENSE"

    def __post_init__(self) -> None:
        if self.acceptance_required and not self.restrictions:
            raise AssertionError("acceptance cannot be required with nothing to accept")


@dataclass(frozen=True, slots=True)
class MinimumBudget:
    """The floor F-84 calls the minimum budget a model is approved to run in.

    Deliberately not a :class:`~echoact.domain.Budget`: that type is the
    ceiling actually applied to a job and carries thread counts derived from
    it.  This is a claim about approval, and comparing the two is
    ``registry.can_run``'s job, which F-04 requires to answer with a reason.
    """

    memory_bytes: int
    cpu_percent: int

    def fits(self, budget: Budget) -> bool:
        return budget.memory_bytes >= self.memory_bytes and budget.cpu_percent >= self.cpu_percent


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One model, completely described (F-84)."""

    model_id: str
    display_name: str
    #: Source repository, and the commit the digests below were taken at.  A
    #: branch name would let the upstream redefine what "sound" means.
    repo_id: str
    revision: str
    files: tuple[ModelFile, ...]
    license: LicenseTerms
    #: F-82 forbids resampling, so this is the rate of every result.
    sample_rate: int
    languages: tuple[Language, ...]
    minimum_budget: MinimumBudget
    voices: tuple[VoiceEntry, ...]
    #: What the app asks the engine to load, when that differs from the id.
    engine_model_name: str = ""

    def __post_init__(self) -> None:
        if not self.files:
            raise AssertionError(f"{self.model_id}: a manifest entry with no files")
        paths = [f.relative_path for f in self.files]
        if len(set(paths)) != len(paths):
            raise AssertionError(f"{self.model_id}: duplicate file path in manifest")
        ids = [v.voice_id for v in self.voices]
        if len(set(ids)) != len(ids):
            raise AssertionError(f"{self.model_id}: duplicate voice id")
        if Language.AUTO in self.languages:
            # F-05's AUTO is a request-time mode resolved per sentence; it is
            # never something a model "supports".
            raise AssertionError(f"{self.model_id}: AUTO is not a model language")
        if self.sample_rate <= 0:
            raise AssertionError(f"{self.model_id}: bad sample rate")

    @property
    def total_bytes(self) -> int:
        """What F-63 reports as required storage before a download."""
        return sum(f.byte_size for f in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def engine_name(self) -> str:
        return self.engine_model_name or self.model_id

    def file(self, relative_path: str) -> ModelFile | None:
        for f in self.files:
            if f.relative_path == relative_path:
                return f
        return None

    def supports_language(self, language: Language) -> bool:
        """``AUTO`` is supported wherever anything is (F-05)."""
        if language is Language.AUTO:
            return bool(self.languages)
        return language in self.languages

    def voices_for(self, gender: Gender) -> tuple[VoiceEntry, ...]:
        return tuple(v for v in self.voices if v.gender is gender)

    def voice(self, voice_id: str) -> VoiceEntry:
        for v in self.voices:
            if v.voice_id == voice_id:
                return v
        raise EchoActError(
            Code.VOICE_UNKNOWN, detail={"model_id": self.model_id, "voice_id": voice_id}
        )

    def validate_voice(self, voice_id: str, gender: Gender) -> VoiceEntry:
        """F-06: the voice must exist *and* belong to the requested gender.

        Two codes rather than one, because "no such voice" and "that voice is
        the other gender" are different mistakes for a caller to fix, and
        F-57 makes the code the thing a client branches on.
        """
        voice = self.voice(voice_id)
        if voice.gender is not gender:
            raise EchoActError(
                Code.VOICE_GENDER_MISMATCH,
                detail={
                    "voice_id": voice_id,
                    "voice_gender": voice.gender.value,
                    "requested_gender": gender.value,
                },
            )
        return voice


@dataclass(frozen=True, slots=True)
class Manifest:
    """The lineup this build ships.

    F-04 keeps the selection machinery even with one model, so that a second
    one is data rather than structure; this container is that machinery's
    read side.
    """

    models: tuple[ModelEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ids = [m.model_id for m in self.models]
        if len(set(ids)) != len(ids):
            raise AssertionError("duplicate model id in manifest")

    def __iter__(self) -> Iterator[ModelEntry]:
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(m.model_id for m in self.models)

    def has(self, model_id: str) -> bool:
        return any(m.model_id == model_id for m in self.models)

    def get(self, model_id: str) -> ModelEntry:
        for m in self.models:
            if m.model_id == model_id:
                return m
        raise EchoActError(Code.MODEL_UNKNOWN, detail={"model_id": model_id})


__all__ = [
    "LicenseTerms",
    "Manifest",
    "MinimumBudget",
    "ModelEntry",
    "ModelFile",
    "VoiceEntry",
]
