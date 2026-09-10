"""The shipped manifest (F-84): one model, Supertonic 3.

A.3 fixes the lineup at a single model, and F-04 keeps the selection
machinery anyway so that a second one is data rather than structure.  This
file is that data.

It is a Python literal on purpose.  F-84 says manifest changes ship with the
application and are never applied as a silent remote update; a manifest that
could be fetched would let whoever served it redefine which bytes count as
sound for weights already on this disk.  Changing anything here is a release.

The digests below were computed over the files of
``Supertone/supertonic-3`` at the pinned revision, on disk, with SHA-256 --
not copied from a listing.  ``tests/test_manifest.py`` re-derives them from
the real weights when they are present, because F-84 makes a mismatch mean
"corrupted": an invented digest would turn every honest copy into a fake
corruption report, which is worse than shipping no manifest at all.

The licence text is quoted from the ``LICENSE`` file of that same revision.
N-11 treats the pass-through clause as a release blocker, so it is recorded
as an obligation the product must carry to its own users, not as a notice.
"""

from __future__ import annotations

from typing import Final

from ..domain import Gender, Language
from ..policy import CPU_PERCENT_MIN, MEMORY_FLOOR_BYTES
from .manifest import (
    LicenseTerms,
    Manifest,
    MinimumBudget,
    ModelEntry,
    ModelFile,
    VoiceEntry,
)

SUPERTONIC_3_ID: Final = "supertonic-3"

#: Attachment A of the BigScience OpenRAIL-M licence, quoted from the
#: ``LICENSE`` file at the pinned revision, in its own order and wording.
#: N-11 requires these to reach the end user unchanged, and a paraphrase is
#: what a reader would hold us to instead of the real term.
_USE_RESTRICTIONS: Final[tuple[str, ...]] = (
    "(a) In any way that violates any applicable national, federal, state, local or "
    "international law or regulation;",
    "(b) For the purpose of exploiting, harming or attempting to exploit or harm minors in "
    "any way;",
    "(c) To generate or disseminate verifiably false information and/or content with the "
    "purpose of harming others;",
    "(d) To generate or disseminate personal identifiable information that can be used to "
    "harm an individual;",
    "(e) To generate or disseminate information and/or content (e.g. images, code, posts, "
    "articles), and place the information and/or content in any context (e.g. bot generating "
    "tweets) without expressly and intelligibly disclaiming that the information and/or "
    "content is machine generated;",
    "(f) To defame, disparage or otherwise harass others;",
    "(g) To impersonate or attempt to impersonate (e.g. deepfakes) others without their "
    "consent;",
    "(h) For fully automated decision making that adversely impacts an individual’s legal "
    "rights or otherwise creates or modifies a binding, enforceable obligation;",
    "(i) For any use intended to or which has the effect of discriminating against or harming "
    "individuals or groups based on online or offline social behavior or known or predicted "
    "personal or personality characteristics;",
    "(j) To exploit any of the vulnerabilities of a specific group of persons based on their "
    "age, social, physical or mental characteristics, in order to materially distort the "
    "behavior of a person pertaining to that group in a manner that causes or is likely to "
    "cause that person or another person physical or psychological harm;",
    "(k) For any use intended to or which has the effect of discriminating against individuals "
    "or groups based on legally protected characteristics or categories;",
    "(l) To provide medical advice and medical results interpretation;",
    "(m) To generate or disseminate information for the purpose to be used for administration "
    "of justice, law enforcement, immigration or asylum processes, such as predicting an "
    "individual will commit fraud/crime commitment (e.g. by text profiling, drawing causal "
    "relationships between assertions made in documents, indiscriminate and "
    "arbitrarily-targeted use).",
)

SUPERTONIC_3_LICENSE: Final = LicenseTerms(
    name="BigScience OpenRAIL-M",
    restrictions=_USE_RESTRICTIONS,
    # Paragraph 5, quoted.  This sentence is why N-11 calls shipping the
    # model without carrying the restrictions through a release blocker.
    pass_through_obligation=(
        "Paragraph 5, Use-based restrictions: “You shall require all of Your users who use "
        "the Model or a Derivative of the Model to comply with the terms of this paragraph "
        "(paragraph 5).” The restrictions in Attachment A therefore bind everyone who uses "
        "EchoAct's speech generation, not only whoever installed it."
    ),
    acceptance_required=True,
    notes=(
        "Full title as it appears in the file: “BigScience Open RAIL-M License, dated "
        "August 18, 2022”.",
        "Paragraph 6, the Output: “Except as set forth herein, Licensor claims no rights in "
        "the Output You generate using the Model. You are accountable for the Output you "
        "generate and its subsequent uses. No use of the output can contravene any provision "
        "as stated in the License.”",
        "Paragraph 7, Updates and Runtime Restrictions: the licensor “reserves the right to "
        "restrict (remotely or otherwise) usage of the Model” and asks that You “undertake "
        "reasonable efforts to use the latest version of the Model”. EchoAct implements no "
        "remote control and checks for updates only on the user's action (N-01, F-75); A.4 "
        "leaves reading this clause against a deliberately offline product to a lawyer.",
        "Being able to run the model locally is not a right to redistribute it (N-11); the "
        "weights are downloaded from the source repository, never bundled.",
    ),
    source_file="LICENSE",
)

#: Every voice description ends with this.  A.4 records that nobody has
#: listened to this model in either language yet, so a description that
#: claimed a character would be invented.  F-53 still needs a caller to have
#: something to choose on, so the descriptions carry what is verifiable and
#: say plainly that the rest is unknown.
_UNREVIEWED: Final = (
    "Its voice character has not been reviewed yet, so nothing is claimed here about tone, "
    "age or intended use (N-08)."
)


def _voice(voice_id: str, gender: Gender, index: int, count: int) -> VoiceEntry:
    """One built-in style, described only as far as it has been verified."""
    word = "male" if gender is Gender.MALE else "female"
    return VoiceEntry(
        voice_id=voice_id,
        gender=gender,
        display_name=f"{word.capitalize()} {index}",
        description=(
            f"Built-in Supertonic 3 style {voice_id}: {word} voice {index} of {count}. "
            f"Reads both Korean and English, since a voice is not specific to one language "
            f"(F-06). {_UNREVIEWED}"
        ),
        characterised=False,
    )


SUPERTONIC_3_VOICES: Final[tuple[VoiceEntry, ...]] = (
    _voice("M1", Gender.MALE, 1, 5),
    _voice("M2", Gender.MALE, 2, 5),
    _voice("M3", Gender.MALE, 3, 5),
    _voice("M4", Gender.MALE, 4, 5),
    _voice("M5", Gender.MALE, 5, 5),
    _voice("F1", Gender.FEMALE, 1, 5),
    _voice("F2", Gender.FEMALE, 2, 5),
    _voice("F3", Gender.FEMALE, 3, 5),
    _voice("F4", Gender.FEMALE, 4, 5),
    _voice("F5", Gender.FEMALE, 5, 5),
)

#: The four ONNX graphs and two tables the runtime loads, plus one style
#: file per voice.  Nothing else in the repository is needed to synthesise,
#: so nothing else is downloaded: F-73 sizes this cache and N-01 keeps the
#: network traffic to what preparation actually requires.
SUPERTONIC_3_FILES: Final[tuple[ModelFile, ...]] = (
    ModelFile(
        "onnx/duration_predictor.onnx",
        "c3eb91414d5ff8a7a239b7fe9e34e7e2bf8a8140d8375ffb14718b1c639325db",
        3_700_147,
    ),
    ModelFile(
        "onnx/text_encoder.onnx",
        "c7befd5ea8c3119769e8a6c1486c4edc6a3bc8365c67621c881bbb774b9902ff",
        36_416_150,
    ),
    ModelFile(
        "onnx/vector_estimator.onnx",
        "883ac868ea0275ef0e991524dc64f16b3c0376efd7c320af6b53f5b780d7c61c",
        256_534_781,
    ),
    ModelFile(
        "onnx/vocoder.onnx",
        "085de76dd8e8d5836d6ca66826601f615939218f90e519f70ee8a36ed2a4c4ba",
        101_424_195,
    ),
    ModelFile(
        "onnx/tts.json",
        "42078d3aef1cd43ab43021f3c54f47d2d75ceb4e75f627f118890128b06a0d09",
        8_253,
    ),
    ModelFile(
        "onnx/unicode_indexer.json",
        "9bf7346e43883a81f8645c81224f786d43c5b57f3641f6e7671a7d6c493cb24f",
        277_676,
    ),
    ModelFile(
        "voice_styles/M1.json",
        "e35604687f5d23694b8e91593a93eec0e4eca6c0b02bb8ed69139ab2ea6b0a5b",
        291_748,
    ),
    ModelFile(
        "voice_styles/M2.json",
        "b76cbf62bac707c710cf0ae5aba5e31eea1a6339a9734bfae33ab98499534a50",
        292_055,
    ),
    ModelFile(
        "voice_styles/M3.json",
        "ea1ac35ccb91b0d7ecad533a2fbd0eec10c91513d8951e3b25fbba99954e159b",
        290_198,
    ),
    ModelFile(
        "voice_styles/M4.json",
        "ca8eefad4fcd989c9379032ff3e50738adc547eeb5e221b82593a6d7b3bac303",
        291_522,
    ),
    ModelFile(
        "voice_styles/M5.json",
        "dd22b92740314321f8ae11c5e87f8dd60d060f15dd3a632b5adf77f471f77af2",
        291_469,
    ),
    ModelFile(
        "voice_styles/F1.json",
        "bbdec6ee00231c2c742ad05483df5334cab3b52fda3ba38e6a07059c4563dbc2",
        292_046,
    ),
    ModelFile(
        "voice_styles/F2.json",
        "7c722c6a72707b1a77f035d67f0d1351ba187738e06f7683e8c72b1df3477fc6",
        292_423,
    ),
    ModelFile(
        "voice_styles/F3.json",
        "12f6ef2573baa2defa1128069cb59f203e3ab67c92af77b42df8a0e3a2f7c6ab",
        290_794,
    ),
    ModelFile(
        "voice_styles/F4.json",
        "c2fa764c1225a76dfc3e2c73e8aa4f70d9ee48793860eb34c295fff01c2e032b",
        291_808,
    ),
    ModelFile(
        "voice_styles/F5.json",
        "45966e73316415626cf41a7d1c6f3b4c70dbc1ba2bee5c1978ef0ce33244fc8d",
        291_479,
    ),
)

SUPERTONIC_3: Final = ModelEntry(
    model_id=SUPERTONIC_3_ID,
    display_name="Supertonic 3",
    repo_id="Supertone/supertonic-3",
    revision="724fb5abbf5502583fb520898d45929e62f02c0b",
    files=SUPERTONIC_3_FILES,
    license=SUPERTONIC_3_LICENSE,
    # A.5 measured 44,100 Hz mono, which F-82 then forbids resampling away.
    sample_rate=44_100,
    # The engine advertises 31 languages; the product supports two (F-05),
    # and this list is what F-53 answers and what a request is checked
    # against.  Claiming the other 29 would promise pronunciation nobody has
    # tested and N-08 has not reviewed even for these two.
    languages=(Language.KO, Language.EN),
    # F-23's floor is also this model's approved minimum: A.5 measured 0.57
    # GB of working memory, so 2 GiB is comfortable rather than tight.  At
    # F-20's lowest CPU setting the baseline is about one thread, which A.5
    # measured at a real-time factor under 0.36 -- still faster than
    # playback, so the model is approved down to the bottom of the range.
    minimum_budget=MinimumBudget(memory_bytes=MEMORY_FLOOR_BYTES, cpu_percent=CPU_PERCENT_MIN),
    voices=SUPERTONIC_3_VOICES,
    engine_model_name="supertonic-3",
)

#: The lineup this build ships.  F-04: one model, chosen through the same
#: machinery a longer list would use.
MANIFEST: Final = Manifest((SUPERTONIC_3,))

DEFAULT_MODEL_ID: Final = SUPERTONIC_3_ID

__all__ = [
    "DEFAULT_MODEL_ID",
    "MANIFEST",
    "SUPERTONIC_3",
    "SUPERTONIC_3_ID",
    "SUPERTONIC_3_LICENSE",
]
