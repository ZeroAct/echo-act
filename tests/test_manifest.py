"""The shipped manifest is a factual claim about bytes on a server (F-84).

These tests check the two things a manifest can be wrong about: the facts it
states (F-84's fields, F-05's languages, F-06's voices, N-11's licence) and
whether those facts match reality.  The second group needs the real weights
and is marked ``engine``; without them the manifest is still checked for
internal consistency, because a manifest that contradicts itself is a bug
whether or not the weights are on this machine.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from echoact.domain import Gender, Language
from echoact.errors import Code, EchoActError
from echoact.models.catalog import DEFAULT_MODEL_ID, MANIFEST, SUPERTONIC_3
from echoact.models.manifest import (
    LicenseTerms,
    Manifest,
    MinimumBudget,
    ModelEntry,
    ModelFile,
    VoiceEntry,
)
from echoact.policy import CPU_PERCENT_MIN, GIB, MEMORY_FLOOR_BYTES

#: Where the weights already sit on a developer machine, per CLAUDE.md.
WEIGHTS_DIR = Path.home() / ".cache" / "supertonic3"

requires_weights = pytest.mark.skipif(
    not WEIGHTS_DIR.is_dir(), reason="Supertonic 3 weights are not present on this machine"
)


# ----------------------------------------------------------------------
# What the manifest says
# ----------------------------------------------------------------------


def test_exactly_one_model_ships_and_it_is_the_default() -> None:
    assert MANIFEST.model_ids == ("supertonic-3",)
    assert DEFAULT_MODEL_ID == "supertonic-3"
    assert MANIFEST.get(DEFAULT_MODEL_ID) is SUPERTONIC_3


def test_the_revision_is_pinned_to_a_commit_rather_than_a_branch() -> None:
    # A branch name would let the upstream change what "sound" means for
    # files already on disk, which is the silent remote update F-84 forbids.
    assert SUPERTONIC_3.revision == "724fb5abbf5502583fb520898d45929e62f02c0b"
    assert re.fullmatch(r"[0-9a-f]{40}", SUPERTONIC_3.revision)
    assert SUPERTONIC_3.repo_id == "Supertone/supertonic-3"


def test_every_file_carries_a_digest_and_a_size() -> None:
    assert SUPERTONIC_3.file_count == 16
    paths = [f.relative_path for f in SUPERTONIC_3.files]
    assert len(set(paths)) == len(paths)
    for f in SUPERTONIC_3.files:
        assert re.fullmatch(r"[0-9a-f]{64}", f.sha256), f.relative_path
        assert f.byte_size > 0


def test_the_manifest_lists_the_four_graphs_and_a_style_file_per_voice() -> None:
    paths = {f.relative_path for f in SUPERTONIC_3.files}
    assert {
        "onnx/duration_predictor.onnx",
        "onnx/text_encoder.onnx",
        "onnx/vector_estimator.onnx",
        "onnx/vocoder.onnx",
        "onnx/tts.json",
        "onnx/unicode_indexer.json",
    } <= paths
    for voice in SUPERTONIC_3.voices:
        assert f"voice_styles/{voice.voice_id}.json" in paths


def test_total_size_is_the_sum_of_the_files_f63_will_ask_the_user_for() -> None:
    assert SUPERTONIC_3.total_bytes == sum(f.byte_size for f in SUPERTONIC_3.files)
    # A.5 recorded 0.38 GB on disk; anything far from that means the file
    # list drifted from what is actually downloaded.
    assert 0.35e9 < SUPERTONIC_3.total_bytes < 0.45e9


def test_native_sample_rate_is_the_one_f82_refuses_to_resample() -> None:
    assert SUPERTONIC_3.sample_rate == 44_100


def test_only_the_two_languages_the_product_supports_are_claimed() -> None:
    # The engine advertises 31; F-05 supports two, and F-53 answers from here.
    assert SUPERTONIC_3.languages == (Language.KO, Language.EN)
    assert SUPERTONIC_3.supports_language(Language.KO)
    assert SUPERTONIC_3.supports_language(Language.EN)
    # AUTO is a request-time mode, resolved per sentence, not a model claim.
    assert SUPERTONIC_3.supports_language(Language.AUTO)


def test_ten_voices_five_of_each_gender() -> None:
    assert len(SUPERTONIC_3.voices) == 10
    assert [v.voice_id for v in SUPERTONIC_3.voices_for(Gender.MALE)] == [
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
    ]
    assert [v.voice_id for v in SUPERTONIC_3.voices_for(Gender.FEMALE)] == [
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
    ]


def test_every_voice_gives_a_non_human_caller_something_to_choose_on() -> None:
    for voice in SUPERTONIC_3.voices:
        assert voice.display_name
        assert len(voice.description) > 40, voice.voice_id
        assert voice.voice_id in voice.description
        expected_gender = "male" if voice.gender is Gender.MALE else "female"
        assert expected_gender in voice.description.lower()


def test_no_voice_claims_a_character_nobody_has_listened_to() -> None:
    # A.4 keeps N-08 open: nobody has heard this model in either language.
    # A description that called a voice "warm" would be invented, and F-53's
    # caller would choose on it.
    invented = (
        "warm",
        "authoritative",
        "friendly",
        "soothing",
        "energetic",
        "cheerful",
        "confident",
        "gentle",
        "crisp",
        "youthful",
        "mature",
        "expressive",
        "professional",
        "clear",
        "rich",
        "smooth",
    )
    for voice in SUPERTONIC_3.voices:
        lowered = voice.description.lower()
        for word in invented:
            assert word not in lowered, f"{voice.voice_id} claims {word!r}"
        assert voice.characterised is False
        assert "not been reviewed" in lowered


def test_the_licence_restrictions_are_carried_through_as_accepted_terms() -> None:
    terms = SUPERTONIC_3.license
    assert terms.name == "BigScience OpenRAIL-M"
    # N-11: shipping this model without passing the restrictions on is a
    # release blocker, so acceptance is required before first preparation.
    assert terms.acceptance_required is True
    assert len(terms.restrictions) == 13
    assert terms.restrictions[0].startswith("(a)")
    assert terms.restrictions[-1].startswith("(m)")
    assert "paragraph 5" in terms.pass_through_obligation
    assert any("remotely" in note for note in terms.notes)


def test_minimum_budget_is_the_floor_f23_names_and_the_lowest_cpu_setting() -> None:
    assert SUPERTONIC_3.minimum_budget == MinimumBudget(
        memory_bytes=MEMORY_FLOOR_BYTES, cpu_percent=CPU_PERCENT_MIN
    )
    assert SUPERTONIC_3.minimum_budget.memory_bytes == 2 * GIB


# ----------------------------------------------------------------------
# What the types refuse
# ----------------------------------------------------------------------


def test_an_unknown_model_is_refused_with_its_own_code() -> None:
    with pytest.raises(EchoActError) as caught:
        MANIFEST.get("whisper-9")
    assert caught.value.code is Code.MODEL_UNKNOWN
    assert caught.value.retryable is False
    assert MANIFEST.has("whisper-9") is False


def test_an_unknown_voice_and_a_gender_mismatch_are_different_codes() -> None:
    with pytest.raises(EchoActError) as unknown:
        SUPERTONIC_3.voice("Q9")
    assert unknown.value.code is Code.VOICE_UNKNOWN

    with pytest.raises(EchoActError) as mismatch:
        SUPERTONIC_3.validate_voice("M1", Gender.FEMALE)
    assert mismatch.value.code is Code.VOICE_GENDER_MISMATCH
    assert SUPERTONIC_3.validate_voice("M1", Gender.MALE).voice_id == "M1"


@pytest.mark.parametrize("bad", ["../escape.onnx", "/absolute.onnx", "a/../../b.onnx", ""])
def test_a_manifest_path_may_not_escape_the_model_directory(bad: str) -> None:
    with pytest.raises(AssertionError):
        ModelFile(bad, "0" * 64, 10)


@pytest.mark.parametrize("bad", ["", "abc", "0" * 63, "Z" * 64, "0" * 65])
def test_a_digest_that_is_not_sha256_is_a_packaging_error(bad: str) -> None:
    with pytest.raises(AssertionError):
        ModelFile("onnx/a.onnx", bad, 10)


def test_a_file_path_resolves_under_the_directory_it_is_given() -> None:
    root = Path("/tmp/models/supertonic-3")
    resolved = ModelFile("voice_styles/F1.json", "a" * 64, 10).path_under(root)
    assert resolved == root / "voice_styles" / "F1.json"


def test_duplicate_ids_are_refused_rather_than_shadowed() -> None:
    entry = _minimal_entry("dup")
    with pytest.raises(AssertionError):
        Manifest((entry, _minimal_entry("dup")))
    with pytest.raises(AssertionError):
        ModelEntry(
            model_id="x",
            display_name="X",
            repo_id="o/x",
            revision="0" * 40,
            files=(ModelFile("a.bin", "a" * 64, 1),),
            license=_terms(),
            sample_rate=16_000,
            languages=(Language.KO,),
            minimum_budget=MinimumBudget(GIB, 10),
            voices=(
                VoiceEntry("V1", Gender.MALE, "V1", "a voice"),
                VoiceEntry("V1", Gender.FEMALE, "V1", "a voice"),
            ),
        )


def test_auto_is_not_something_a_model_can_claim_to_support() -> None:
    with pytest.raises(AssertionError):
        ModelEntry(
            model_id="x",
            display_name="X",
            repo_id="o/x",
            revision="0" * 40,
            files=(ModelFile("a.bin", "a" * 64, 1),),
            license=_terms(),
            sample_rate=16_000,
            languages=(Language.AUTO,),
            minimum_budget=MinimumBudget(GIB, 10),
            voices=(),
        )


def test_acceptance_cannot_be_required_with_nothing_to_accept() -> None:
    with pytest.raises(AssertionError):
        LicenseTerms(
            name="Nothing",
            restrictions=(),
            pass_through_obligation="",
            acceptance_required=True,
        )


# ----------------------------------------------------------------------
# Whether the manifest matches reality (needs the real weights)
# ----------------------------------------------------------------------


@pytest.mark.engine
@requires_weights
def test_every_shipped_digest_matches_the_real_file() -> None:
    # F-84 makes a mismatch mean "corrupted", so an invented digest would
    # turn every honest copy of this model into a false corruption report.
    for entry_file in SUPERTONIC_3.files:
        path = entry_file.path_under(WEIGHTS_DIR)
        assert path.is_file(), entry_file.relative_path
        assert path.stat().st_size == entry_file.byte_size, entry_file.relative_path
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        assert digest.hexdigest() == entry_file.sha256, entry_file.relative_path


@pytest.mark.engine
@requires_weights
def test_the_quoted_use_restrictions_are_the_licence_s_own_words() -> None:
    # N-11 requires the restrictions to reach the user; a paraphrase is what
    # a reader would hold us to instead of the real term.
    licence = _collapse((WEIGHTS_DIR / "LICENSE").read_text(encoding="utf-8"))
    for restriction in SUPERTONIC_3.license.restrictions:
        assert _collapse(restriction) in licence, restriction[:40]
    assert (
        _collapse(
            "You shall require all of Your users who use the Model or a Derivative of the "
            "Model to comply with the terms of this paragraph (paragraph 5)."
        )
        in licence
    )


# ----------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _terms() -> LicenseTerms:
    return LicenseTerms(
        name="Test",
        restrictions=("(a) Do no harm.",),
        pass_through_obligation="paragraph 5",
        acceptance_required=True,
    )


def _minimal_entry(model_id: str) -> ModelEntry:
    return ModelEntry(
        model_id=model_id,
        display_name=model_id,
        repo_id=f"org/{model_id}",
        revision="0" * 40,
        files=(ModelFile("a.bin", "a" * 64, 1),),
        license=_terms(),
        sample_rate=16_000,
        languages=(Language.KO,),
        minimum_budget=MinimumBudget(GIB, 10),
        voices=(VoiceEntry("V1", Gender.MALE, "V1", "a voice"),),
    )
