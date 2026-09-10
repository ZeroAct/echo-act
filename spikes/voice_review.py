"""Produce the Section 8.2 review material, and measure what can be measured.

A.4's first release blocker is that nobody has listened to this model in
either language, and Section 8.3 makes missing speech, unnecessary
repetition, corrupted audio, and clearly incorrect sentence output
release-blocking defects.  Listening is a human exercise, so this spike does
the part a machine can do:

1. Renders the Section 8.2 sample -- 20 Korean and 20 English sentences plus
   mixed language, numbers, dates, currency, abbreviations, symbols and
   repeated sentences -- so there is something to listen to.
2. Renders one short line per voice, in both languages, so the ten voices can
   be compared without wading through the whole set.
3. Measures objective descriptors per voice: median fundamental frequency,
   spectral centroid, and speaking rate.  F-53 requires every voice to carry
   a description, and A.4 forbids inventing a character for a voice nobody
   has heard; a measured descriptor is neither invented nor a claim about
   how the voice sounds to a person.
4. Flags what a machine *can* detect of Section 8.3's defects: silence,
   clipping, and a duration wildly out of line with the text length.

    uv run python spikes/voice_review.py

Writes WAV files and a report to spikes/_out/voices/.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "_out" / "voices"
SR = 44100

KO = [
    "에코액트는 문서를 소리내어 읽어 줍니다.",
    "재생 중인 문장은 화면에서 강조됩니다.",
    "첫 문장이 준비되는 즉시 재생이 시작됩니다.",
    "이 프로그램은 인터넷 연결 없이도 동작합니다.",
    "생성된 음성은 웨이브 파일로 저장할 수 있습니다.",
    "설정에서 목소리와 말하기 속도를 바꿀 수 있습니다.",
    "오늘 회의는 오후 세 시에 시작합니다.",
    "지난 분기 매출은 전년 대비 십이 퍼센트 증가했습니다.",
    "그는 문을 열고 조용히 방으로 들어갔다.",
    "봄이 오면 강가에 벚꽃이 피어난다.",
    "정말요? 그럴 리가 없는데요.",
    "잠깐만요, 다시 한번 말씀해 주시겠어요?",
    "책상 위에는 책과 연필, 그리고 지우개가 놓여 있었다.",
    "이번 주말에는 비가 온다고 합니다.",
    "한국어와 영어를 모두 읽을 수 있습니다.",
    "긴 문장은 쉼표나 접속사에서 나누어 처리되며, 그렇게 해야 재생이 더 빨리 시작되고, 각 구간의 시간도 정확히 알 수 있습니다.",
    "아주 짧은 문장.",
    "네.",
    "감사합니다. 좋은 하루 되세요.",
    "다음 문단으로 넘어가겠습니다.",
]

EN = [
    "EchoAct reads your documents aloud.",
    "The sentence being played is emphasised on screen.",
    "Playback starts as soon as the first sentence is ready.",
    "This program works without an internet connection.",
    "Generated speech can be saved as a WAV file.",
    "You can change the voice and the speaking rate in settings.",
    "Today's meeting starts at three in the afternoon.",
    "Revenue rose twelve percent compared with last year.",
    "He opened the door and walked quietly into the room.",
    "When spring comes, cherry blossoms line the riverbank.",
    "Really? That can't be right.",
    "Hold on, could you say that again?",
    "On the desk there were books, a pencil, and an eraser.",
    "They say it will rain this weekend.",
    "It can read both Korean and English.",
    "A long sentence is split at commas or conjunctions, which is what lets playback begin sooner, and it is also what makes each segment's timing exactly known.",
    "A very short sentence.",
    "Yes.",
    "Thank you, and have a good day.",
    "Let us move on to the next paragraph.",
]

TRICKY = [
    ("mixed", "에코액트는 REST API를 설명합니다. It runs entirely on this machine."),
    ("numbers-ko", "2026년 9월 10일, 1,234원, 3.14, 44.1kHz, 12.5 퍼센트입니다."),
    ("numbers-en", "On 2026-09-10 it cost $1,234.50, or about 12.5% more than 3.14 units."),
    ("abbrev-en", "Dr. Kim met Mr. Park at 3 p.m., i.e. after lunch, vs. the usual 9 a.m."),
    ("symbols", "괄호(그리고) 따옴표 \"인용\" 물음표? 느낌표! ... 줄임표"),
    ("repeated-ko", "같은 문장입니다. 같은 문장입니다. 같은 문장입니다."),
    ("repeated-en", "The same sentence. The same sentence. The same sentence."),
    ("units", "The file is 385 MB and the model needs 2 GiB of RAM at 44.1 kHz."),
]

SHORT_KO = "안녕하세요. 에코액트입니다."
SHORT_EN = "Hello. This is EchoAct."


def measure(samples, sample_rate: int) -> dict:
    """Objective descriptors, and the defects a machine can see.

    Pitch is estimated by autocorrelation over voiced frames.  Crude next to
    a real tracker, but the number only has to separate one built-in voice
    from another, not to be a research measurement -- and being crude is
    better than being a claim about how a voice sounds, which nobody has
    verified.
    """
    import numpy as np

    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return {"error": "no samples"}

    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x)))
    clipped = int(np.count_nonzero(np.abs(x) >= 0.999))

    frame, hop = 1024, 512
    lo, hi = int(sample_rate / 400), int(sample_rate / 70)  # 70-400 Hz
    pitches: list[float] = []
    centroids: list[float] = []
    voiced = 0
    total = 0
    window = np.hanning(frame).astype(np.float32)
    for start in range(0, max(0, x.size - frame), hop):
        seg = x[start : start + frame]
        total += 1
        energy = float(np.sqrt(np.mean(seg * seg)))
        if energy < 0.01:
            continue
        voiced += 1
        w = seg * window
        spec = np.abs(np.fft.rfft(w))
        freqs = np.fft.rfftfreq(frame, 1 / sample_rate)
        if spec.sum() > 0:
            centroids.append(float((spec * freqs).sum() / spec.sum()))
        ac = np.correlate(w, w, mode="full")[frame - 1 :]
        if hi < ac.size:
            lag = int(np.argmax(ac[lo:hi])) + lo
            if ac[lag] > 0.3 * ac[0]:
                pitches.append(sample_rate / lag)

    return {
        "duration_s": round(x.size / sample_rate, 3),
        "peak": round(peak, 4),
        "rms": round(rms, 5),
        "clipped_samples": clipped,
        "voiced_fraction": round(voiced / total, 3) if total else 0.0,
        "f0_median_hz": round(statistics.median(pitches), 1) if pitches else None,
        "f0_iqr_hz": (
            round(statistics.quantiles(pitches, n=4)[2] - statistics.quantiles(pitches, n=4)[0], 1)
            if len(pitches) > 8
            else None
        ),
        "centroid_median_hz": round(statistics.median(centroids), 1) if centroids else None,
        "silent": bool(rms < 1e-4),
    }


def main() -> int:
    import numpy as np
    import supertonic as s

    OUT.mkdir(parents=True, exist_ok=True)
    tts = s.TTS(
        model="supertonic-3", auto_download=False, intra_op_num_threads=2, inter_op_num_threads=1
    )
    voices = sorted(tts.voice_style_names)
    print(f"sample rate {tts.sample_rate}, voices {voices}")

    report: dict = {"sample_rate": tts.sample_rate, "voices": {}, "sample_set": [], "flags": []}

    # ---- one short line per voice, both languages -----------------------
    print("\nper-voice comparison lines")
    for name in voices:
        style = tts.get_voice_style(name)
        per_lang = {}
        for lang, text in (("ko", SHORT_KO), ("en", SHORT_EN)):
            wav, _ = tts.synthesize(
                text, style, lang=lang, total_steps=8, speed=1.0,
                max_chunk_length=100000, silence_duration=0.0,
            )
            path = OUT / f"voice-{name}-{lang}.wav"
            tts.save_audio(wav, str(path))
            m = measure(wav, tts.sample_rate)
            m["codepoints"] = len(text)
            m["codepoints_per_second"] = round(len(text) / m["duration_s"], 2)
            per_lang[lang] = m
            if m["silent"]:
                report["flags"].append(f"{name}/{lang}: produced silence")
            if m["clipped_samples"]:
                report["flags"].append(f"{name}/{lang}: {m['clipped_samples']} clipped samples")
        report["voices"][name] = per_lang
        ko, en = per_lang["ko"], per_lang["en"]
        print(
            f"  {name}  f0 ko={ko['f0_median_hz']} en={en['f0_median_hz']} Hz   "
            f"centroid ko={ko['centroid_median_hz']} Hz   "
            f"rate ko={ko['codepoints_per_second']} en={en['codepoints_per_second']} cp/s"
        )

    # ---- the Section 8.2 sample set, on the default voice ---------------
    print("\nSection 8.2 review set (voice F1)")
    style = tts.get_voice_style("F1")
    started = time.perf_counter()
    for lang, sentences in (("ko", KO), ("en", EN)):
        for i, text in enumerate(sentences, 1):
            wav, _ = tts.synthesize(
                text, style, lang=lang, total_steps=8, speed=1.0,
                max_chunk_length=100000, silence_duration=0.0,
            )
            path = OUT / f"set-{lang}-{i:02d}.wav"
            tts.save_audio(wav, str(path))
            m = measure(wav, tts.sample_rate)
            m.update({"lang": lang, "index": i, "text": text, "file": path.name})
            report["sample_set"].append(m)
            cps = len(text) / m["duration_s"]
            # Section 8.3 calls missing speech and corrupted audio
            # release-blocking.  Silence and an implausible rate are the two
            # a machine can see without ears.
            if m["silent"]:
                report["flags"].append(f"{lang}-{i:02d}: silence")
            # Only meaningful once there is enough text for onset and
            # trailing silence to stop dominating: "네." is two code points
            # and a second of audio, and that is not a defect.
            if len(text) >= 12 and not (2.0 < cps < 30.0):
                report["flags"].append(f"{lang}-{i:02d}: {cps:.1f} cp/s is implausible")
            if m["clipped_samples"] > 0:
                report["flags"].append(f"{lang}-{i:02d}: {m['clipped_samples']} clipped samples")
    for name, text in TRICKY:
        lang = "ko" if any("가" <= c <= "힣" for c in text) else "en"
        wav, _ = tts.synthesize(
            text, style, lang=lang, total_steps=8, speed=1.0,
            max_chunk_length=100000, silence_duration=0.0,
        )
        path = OUT / f"set-tricky-{name}.wav"
        tts.save_audio(wav, str(path))
        m = measure(wav, tts.sample_rate)
        m.update({"lang": lang, "index": name, "text": text, "file": path.name})
        report["sample_set"].append(m)
        print(f"  {name:14s} {m['duration_s']:5.2f}s peak={m['peak']:.2f}")
    report["elapsed_s"] = round(time.perf_counter() - started, 1)

    # ---- one file per language, all ten voices in a row ------------------
    #
    # Sixty-eight files is a pile to work through.  Comparing voices is the
    # first thing a listener has to do, and it is much easier back to back.
    print("\ncomparison reels")
    import soundfile as sf

    gap = np.zeros((1, int(0.7 * tts.sample_rate)), dtype=np.float32)
    for lang in ("ko", "en"):
        pieces = []
        for name in voices:
            data, _ = sf.read(str(OUT / f"voice-{name}-{lang}.wav"), dtype="float32")
            pieces.append(np.asarray(data).reshape(1, -1))
            pieces.append(gap)
        reel = np.concatenate(pieces, axis=1)
        path = OUT / f"all-voices-{lang}.wav"
        tts.save_audio(reel, str(path))
        print(f"  {path.name}: {reel.shape[-1] / tts.sample_rate:.1f}s, order {' '.join(voices)}")
        report[f"reel_{lang}"] = {"file": path.name, "order": voices}

    # ---- what the measurements are, and are not, good for ----------------
    order = sorted(voices, key=lambda n: report["voices"][n]["ko"]["f0_median_hz"] or 0)
    report["pitch_order_low_to_high"] = order
    # The estimator disagrees with itself across languages for the same
    # voice -- M2 reads 178 Hz in Korean and 286 Hz in English, a ratio too
    # close to 8/5 to be anything but an octave or harmonic error.  So this
    # ordering is recorded as a measurement artefact, not as a voice
    # description: F-53's descriptions stay as A.4 leaves them until a
    # person has listened.
    disagreements = [
        n
        for n in voices
        if report["voices"][n]["ko"]["f0_median_hz"]
        and report["voices"][n]["en"]["f0_median_hz"]
        and not (
            0.75
            <= report["voices"][n]["en"]["f0_median_hz"]
            / report["voices"][n]["ko"]["f0_median_hz"]
            <= 1.33
        )
    ]
    report["pitch_estimator_disagrees_across_languages"] = disagreements
    report["pitch_usable_for_descriptions"] = not disagreements
    (OUT / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n" + "=" * 66)
    print(f"{len(list(OUT.glob('*.wav')))} files in {OUT}")
    print(f"synthesised the 48-item set in {report['elapsed_s']}s")
    print("pitch order, low to high (Korean):", " ".join(order))
    if disagreements:
        print(
            "pitch estimate is NOT usable as a description: it disagrees with\n"
            "itself across languages for " + ", ".join(disagreements)
        )
    if report["flags"]:
        print("\nMachine-detectable problems:")
        for f in report["flags"]:
            print("  -", f)
    else:
        print("\nNo silence, clipping, or implausible durations.")
    print(
        "\nWhat a machine cannot do: N-08 and Section 8.3 ask for pronunciation,\n"
        "omissions, repetitions and naturalness. Those need ears. The files are\n"
        "there to be listened to."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
