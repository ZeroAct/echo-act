# EchoAct — working notes for contributors

EchoAct generates Korean and English speech from text, entirely on the user's
PC. `docs/design.md` is the requirements baseline and the only authority: every
behaviour traces to an `F-`, `N-`, or `A-` identifier there. Appendix A is a
non-normative planning note; A.2 records the chosen stack and build order, A.3
records decisions that must not be reopened, A.5 records what was measured.

## Where things live

```
echoact/
  errors.py      Code enum + EchoActError. The ONLY exception crossing a module boundary.
  policy.py      Every number Section 4 fixes. Never hard-code a limit elsewhere.
  domain.py      Job/Segment/Result/Document, states, VoiceSettings, Budget, TextRange.
  paths.py       Every on-disk location, plus redact() for logs.
  config/        Persisted settings (F-24), resource budget resolution (F-20/21/23, N-04).
  models/        F-84 manifest, download/verify/repair (F-63..F-65).
  text/          F-81 segmentation, F-27 normalisation-with-alignment, file sniffing (F-32..F-35).
  engine/        F-87 runtime pinning, worker child process, OS resource container (N-03).
  jobs/          Single-slot job engine (F-47), dedup keys (F-49), estimates (F-88).
  audio/         F-82 WAV, PortAudio player with a frame clock (N-12), devices (F-67).
  db/            SQLite WAL store, backup/restore (F-44, N-27).
  security/      Credentials (F-71), scopes (F-61), rate limits (4.1).
  service/       FastAPI, the 12 endpoints in Section 2.10.
  mcp/           FastMCP stdio server — a REST client, nothing more (F-58).
  ui/            PySide6 widgets.
tests/           pytest. Mirror the package layout: tests/text/test_segment.py etc.
```

## Rules that are not negotiable

1. **No number without a home.** Limits, timeouts, defaults, and sizes come
   from `echoact.policy`. If a value is missing there, add it there.
2. **Text offsets are Unicode code points**, half-open, into the job's *source*
   text — never into normalised text, never UTF-16. Qt counts UTF-16; convert
   at the widget boundary and nowhere else (4.2, F-27).
3. **One exception type.** Raise `EchoActError(Code.X, ...)` outward. Do not
   let `sqlite3.Error`, `OSError`, or an engine exception escape a module.
4. **The engine never sees a path or a URL from a client** (N-18). Text a user
   or client supplies is data; it can never reach a style, a voice, or a
   provider selection.
5. **Local CPU only, by allow-list** (F-87, N-01). The installed ONNX Runtime
   offers `AzureExecutionProvider` — this is measured, not hypothetical. Assert
   the session's providers after construction and refuse otherwise.
6. **No body text, audio, or credential in a log** (N-20). Log a job id, a
   stage, a code, and a redacted path.
7. **Never block the Qt main thread.** The engine, the database, and the
   service run off it; the GUI touches widgets only from the main thread.
8. **A retryable error carries a retry-after hint; a permanent one must not**
   (F-57, N-23).

## Style

- Python 3.12, `from __future__ import annotations`, full type hints, `ruff`
  clean at line-length 100.
- Prefer `dataclass(frozen=True, slots=True)` for values, plain classes for
  things with a lifecycle.
- Docstrings say *which requirement* the code answers and *why the obvious
  alternative is wrong*, when that is not evident. Do not restate the code.
- Comments are sparse and load-bearing. No section banners in short files, no
  "# increment counter".
- Tests are named for the behaviour, not the function:
  `test_first_segment_is_capped_so_playback_starts_sooner`.

## Running things

```
uv run python -m echoact          # the app
uv run pytest -q                  # tests
uv run ruff check echoact tests   # lint
```

The Supertonic 3 weights are already cached at `~/.cache/supertonic3`
(revision `724fb5abbf5502583fb520898d45929e62f02c0b`, 385 MB). Tests that need
the real engine must be marked `@pytest.mark.engine` and skipped when the
weights are absent; everything else runs without them.

## Engine facts worth knowing before you touch `engine/`

Measured in `spikes/`, recorded in A.5:

- Supertonic 3: 44,100 Hz mono, ten voices `M1`–`M5` / `F1`–`F5`, warm load
  0.8 s, real-time factor 0.18–0.21 at two threads.
- **More threads is slower.** Twenty threads ran ~2× slower than two.
- `synthesize(silence_duration=...)` does nothing once we chunk text ourselves.
  Inter-segment silence is the app's own to insert (F-82). Pass a large
  `max_chunk_length` so one of our segments is exactly one engine call —
  otherwise the engine re-chunks Korean at 120 characters and our segment time
  ranges stop being exactly known.
- Identical settings do **not** reproduce identical audio (max sample delta
  0.47 on a ±1.0 signal). Nothing may assume a regenerated result matches a
  stored one (F-41).
- The engine vocalises emoji (three emoji → 1.32 s of audio). A.3 decided they
  are not sent for synthesis; their source ranges attach to a neighbour.
