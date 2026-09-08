"""Spike: can the chosen models actually stream on CPU under F-20's budget?

Appendix A.3 of docs/design.md flags this as the largest open risk: if a model
cannot synthesise faster than real time within the Section 8.2 baseline, F-12's
continuous playback does not merely get slower, it stalls at every segment
boundary.  Section 8.3 also requires publishing the minimum budget each model
can run under, so this produces that table.

Section 8.2's baseline is 8 logical CPUs with F-20's default 20 percent cap,
which is about 1.6 cores.  This machine is larger than that, so thread counts
are capped explicitly to emulate the target rather than flatter it; THREADS
below brackets the baseline instead of guessing a single number.

Reported per configuration: real-time factor (synthesis time over audio
duration, under 1.0 being faster than real time), time to first audio, and peak
resident memory.  RTF is what decides F-12; peak RSS is what decides whether
F-21's default budget and F-23's 2 GiB floor admit the model at all.

    uv run --with supertonic --with psutil spikes/tts_feasibility.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import wave

import psutil

# 2 threads is the Section 8.2 baseline (20% of 8 logical CPUs, rounded up).
THREADS = (1, 2, 4, 0)  # 0 means "let onnxruntime use everything"
STEPS = (2, 4, 8)  # supertonic's quality/speed knob; 8 is its default
REPEATS = 3

SHORT_EN = "EchoAct reads your documents aloud, one sentence at a time."
SHORT_KO = "에코액트는 문서를 한 문장씩 소리내어 읽어 줍니다."
LONG_KO = (
    "에코액트는 사용자의 컴퓨터에서 직접 음성을 생성합니다. "
    "외부 서버로 텍스트를 보내지 않으므로 개인정보가 보호됩니다. "
    "생성이 끝나기를 기다리지 않고 첫 문장이 준비되면 재생이 시작됩니다. "
    "읽고 있는 문장은 화면에서 굵게 표시되어 현재 위치를 알 수 있습니다. "
    "노트북에서 다른 작업과 함께 사용할 수 있도록 자원 사용량을 제한합니다."
)
MIXED = (
    "Supertonic 3는 44.1kHz WAV를 출력합니다. "
    "REST API is on by default, bound to 127.0.0.1 only."
)


class PeakRSS:
    """Sample RSS while a block runs; peak RSS is what F-21 has to bound."""

    def __init__(self, proc, hz=50):
        self.proc, self.dt, self.peak, self._stop = proc, 1.0 / hz, 0, False

    def __enter__(self):
        self.peak = self.proc.memory_info().rss
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop:
            try:
                self.peak = max(self.peak, self.proc.memory_info().rss)
            except Exception:
                return
            time.sleep(self.dt)

    def __exit__(self, *a):
        self._stop = True
        self._t.join(timeout=1)


def main() -> int:
    import numpy as np
    import supertonic as s

    proc = psutil.Process()
    vm = psutil.virtual_memory()
    host = {
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "ram_gib": round(vm.total / 2**30, 1),
    }
    print(f"host: {host['logical_cpus']} logical / {host['physical_cpus']} physical "
          f"CPUs, {host['ram_gib']} GiB RAM")
    print("Section 8.2 baseline is 8 logical CPUs at 20% = ~1.6 cores; "
          "read the 2-thread rows as the target.\n")

    # --- voice inventory, for F-06 ---
    probe = s.TTS(model="supertonic-3", auto_download=True, intra_op_num_threads=2)
    voices = {}
    for prefix in ("M", "F"):
        for i in range(1, 9):
            name = f"{prefix}{i}"
            try:
                probe.get_voice_style(name)
                voices.setdefault(prefix, []).append(name)
            except Exception:
                pass
    print(f"voices: male={voices.get('M', [])} female={voices.get('F', [])}")

    # --- sample rate, for F-82 ---
    wav, _ = probe.synthesize(SHORT_EN, probe.get_voice_style("M1"), lang="en")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
    os.makedirs(out, exist_ok=True)
    probe.save_audio(wav, os.path.join(out, "probe.wav"))
    with wave.open(os.path.join(out, "probe.wav")) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
    print(f"output format: {sr} Hz, {ch} channel(s), {sw * 8}-bit\n")
    del probe

    rows = []
    for nthreads in THREADS:
        label = "all" if nthreads == 0 else str(nthreads)
        kw = {} if nthreads == 0 else {"intra_op_num_threads": nthreads,
                                       "inter_op_num_threads": 1}
        t0 = time.perf_counter()
        tts = s.TTS(model="supertonic-3", auto_download=False, **kw)
        load_s = time.perf_counter() - t0
        style = tts.get_voice_style("M1")
        tts.synthesize(SHORT_EN, style, lang="en", total_steps=2)  # warm

        for steps in STEPS:
            for name, text, lang in (
                ("short-en", SHORT_EN, "en"),
                ("short-ko", SHORT_KO, "ko"),
                ("long-ko", LONG_KO, "ko"),
                ("mixed", MIXED, "ko"),
            ):
                times = []
                with PeakRSS(proc) as pk:
                    for _ in range(REPEATS):
                        t = time.perf_counter()
                        wav, _ = tts.synthesize(text, style, lang=lang,
                                                total_steps=steps)
                        times.append(time.perf_counter() - t)
                secs = wav.shape[-1] / sr
                med = statistics.median(times)
                rows.append({
                    "threads": label, "steps": steps, "case": name,
                    "audio_s": round(secs, 2), "synth_s": round(med, 3),
                    "rtf": round(med / secs, 3),
                    "peak_rss_mib": round(pk.peak / 2**20),
                    "load_s": round(load_s, 2),
                })
                r = rows[-1]
                print(f"  threads={label:<3} steps={steps}  {name:<9} "
                      f"audio={r['audio_s']:>5.2f}s synth={r['synth_s']:>6.3f}s "
                      f"RTF={r['rtf']:>6.3f} peakRSS={r['peak_rss_mib']:>5} MiB")
        del tts
        print()

    # --- F-12 streaming: does generation stay ahead of playback? ---
    # The benchmark above synthesises whole texts, but the product synthesises
    # segment by segment (F-81), so per-call overhead and the first segment's
    # length are what actually decide whether playback stalls.  This replays a
    # document the way F-12 would: generate each segment in order, start the
    # clock when the first is ready, and track how far generation stays ahead
    # of the playhead.  A margin that reaches zero is a stall.
    print("F-12 streaming simulation (2 threads, default steps, per segment)")
    tts = s.TTS(model="supertonic-3", auto_download=False,
                intra_op_num_threads=2, inter_op_num_threads=1)
    style = tts.get_voice_style("F1")
    tts.synthesize(SHORT_KO, style, lang="ko", total_steps=2)  # warm

    segs = [x.strip() + "." for x in LONG_KO.split(".") if x.strip()]
    t_start = time.perf_counter()
    produced = 0.0          # seconds of audio generated so far
    t_first = None
    margins = []
    for i, seg in enumerate(segs):
        wav, _ = tts.synthesize(seg, style, lang="ko", total_steps=8)
        produced += wav.shape[-1] / sr
        now = time.perf_counter() - t_start
        if t_first is None:
            t_first = now                      # playback starts here, per F-12
        played = max(0.0, now - t_first)       # playhead position
        margins.append(produced - played)
        print(f"  segment {i + 1}: generated {produced:6.2f}s of audio by "
              f"t={now:5.2f}s, playhead {played:5.2f}s, margin {margins[-1]:+6.2f}s")

    print()
    print(f"  time to first audio: {t_first:.2f}s (N-07)")
    print(f"  minimum buffer margin: {min(margins):+.2f}s")
    print("  STREAMING:", "holds, no stall"
          if min(margins) > 0 else "STALLS, F-12 would wait on the buffer")
    stream = {"time_to_first_audio_s": round(t_first, 2),
              "min_margin_s": round(min(margins), 2),
              "segments": len(segs)}
    del tts
    print()

    with open(os.path.join(out, "tts_feasibility.json"), "w", encoding="utf-8") as f:
        json.dump({"host": host, "sample_rate": sr, "voices": voices,
                   "streaming": stream, "rows": rows}, f, indent=2)

    base = [r for r in rows if r["threads"] == "2" and r["steps"] == 8]
    worst = max(base, key=lambda r: r["rtf"]) if base else None
    if worst:
        print(f"At the Section 8.2 baseline (2 threads, default 8 steps), worst RTF "
              f"= {worst['rtf']} on {worst['case']}, peak RSS "
              f"{worst['peak_rss_mib']} MiB")
        print("VERDICT:", "streams faster than real time"
              if worst["rtf"] < 1.0 else "CANNOT stream; F-12 would stall")
    print("json:", os.path.join(out, "tts_feasibility.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
