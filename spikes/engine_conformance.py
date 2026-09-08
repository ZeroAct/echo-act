"""Spike: does the real engine honour what the requirements promise?

Several requirements name concrete ranges and edge cases that were written
before any engine was chosen.  This checks them against Supertonic 3 as it
actually behaves, so the numbers in the body of docs/design.md are claims the
default model can keep rather than aspirations:

  F-07  tempo across 0.70x to 1.50x
  F-08  inter-sentence pause as a style control
  F-05  per-sentence language selection, including an automatic mode
  F-27  input containing Hangul, Latin, symbols, line breaks and emoji must
        not drop characters or fail; spans that produce no audio are the case
        the amended F-27 has to describe
  F-03  empty and whitespace-only input must be refused, not synthesised
  F-41  identical settings are explicitly not promised to give identical audio

Nothing here judges voice quality; N-08 and the Section 8.2 review sample are
a separate, human exercise.

    uv run --with supertonic --with numpy spikes/engine_conformance.py
"""

from __future__ import annotations

import os
import traceback

SR = 44100


def main() -> int:
    import numpy as np
    import supertonic as s

    tts = s.TTS(model="supertonic-3", auto_download=False,
                intra_op_num_threads=2, inter_op_num_threads=1)
    style = tts.get_voice_style("F1")
    findings = []

    def synth(text, **kw):
        wav, _ = tts.synthesize(text, style, **kw)
        return wav.shape[-1] / SR

    KO = "에코액트는 문서를 소리내어 읽어 줍니다. 두 번째 문장입니다."

    # --- F-07: tempo range -------------------------------------------------
    print("F-07 tempo (spec range 0.70x to 1.50x, default 1.00x)")
    base = None
    for speed in (0.70, 1.00, 1.05, 1.50):
        try:
            d = synth(KO, lang="ko", speed=speed, total_steps=4)
            base = base or d
            print(f"   speed={speed:<5} audio={d:6.2f}s  relative={base / d:5.2f}x")
        except Exception as e:
            print(f"   speed={speed:<5} FAILED {type(e).__name__}: {e}")
            findings.append(f"F-07: tempo {speed} rejected by the engine")

    # --- F-08: inter-sentence pause ---------------------------------------
    print("\nF-08 inter-sentence pause (style presets act on this)")
    prev = None
    for sil in (0.0, 0.3, 0.8):
        d = synth(KO, lang="ko", silence_duration=sil, total_steps=4)
        delta = "" if prev is None else f"  (+{d - prev:.2f}s vs previous)"
        print(f"   silence={sil:<4} audio={d:6.2f}s{delta}")
        prev = d

    # --- F-05: language selection, including automatic --------------------
    print("\nF-05 language selection")
    for lang in ("ko", "en", None):
        try:
            d = synth("에코액트 test 123.", lang=lang, total_steps=4)
            print(f"   lang={str(lang):<5} ok, audio={d:.2f}s")
        except Exception as e:
            print(f"   lang={str(lang):<5} FAILED {type(e).__name__}: {e}")
            findings.append(f"F-05: lang={lang} unsupported")

    # --- F-27: characters that must not break the mapping -----------------
    print("\nF-27 mixed scripts, symbols, line breaks, emoji")
    cases = [
        ("hangul+latin", "에코액트는 REST API를 설명합니다."),
        ("numbers/date", "2026년 9월 8일, 1,234원, 44.1kHz, 3.14."),
        ("symbols", "괄호(그리고) 따옴표 \"인용\" 물음표? 느낌표! ... 줄임표"),
        ("newlines", "첫 번째 줄입니다.\n두 번째 줄입니다.\n\n네 번째 줄입니다."),
        ("emoji inline", "음성 합성 \U0001f3a7 테스트입니다."),
        ("emoji only", "\U0001f3a7\U0001f4d6\U0001f50a"),
        ("whitespace run", "앞　　　뒤 사이에 공백이 많습니다."),
        ("repeated", "같은 문장. 같은 문장. 같은 문장."),
    ]
    for name, text in cases:
        try:
            d = synth(text, lang="ko", total_steps=4)
            note = "  <- produces no audio" if d < 0.15 else ""
            print(f"   {name:<15} audio={d:6.2f}s{note}")
            if d < 0.15:
                findings.append(
                    f"F-27: {name!r} yields {d:.2f}s of audio; the amended F-27 "
                    "rule for zero-audio spans is load-bearing, not theoretical")
        except Exception as e:
            print(f"   {name:<15} FAILED {type(e).__name__}: {e}")
            findings.append(f"F-27: {name!r} raised {type(e).__name__}")

    # --- F-03: empty input must be refused --------------------------------
    print("\nF-03 empty and whitespace-only input")
    for name, text in (("empty", ""), ("spaces", "   "), ("newline", "\n")):
        try:
            d = synth(text, lang="ko", total_steps=2)
            print(f"   {name:<9} returned {d:.2f}s of audio (engine does not refuse)")
            if d > 0.15:
                findings.append(f"F-03: {name!r} produced audio; the app must "
                                "reject it before reaching the engine")
        except Exception as e:
            print(f"   {name:<9} raised {type(e).__name__} (app must catch, per F-25)")

    # --- F-41: identical settings, identical audio? -----------------------
    print("\nF-41 determinism (the spec promises only that it is NOT guaranteed)")
    a, _ = tts.synthesize(KO, style, lang="ko", total_steps=4)
    b, _ = tts.synthesize(KO, style, lang="ko", total_steps=4)
    if a.shape == b.shape:
        same = bool(np.array_equal(a, b))
        peak = float(np.max(np.abs(a - b))) if not same else 0.0
        print(f"   same shape; bit-identical={same}  max sample delta={peak:.2e}")
    else:
        print(f"   different lengths: {a.shape} vs {b.shape}")

    print("\n" + "=" * 62)
    if findings:
        print("Points needing a decision or already covered by an amendment:")
        for f in findings:
            print("  -", f)
    else:
        print("No conformance gaps found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
