# EchoAct

EchoAct reads Korean and English text aloud on your own computer. Nothing you
type is sent anywhere: the model runs on your CPU, and once it is downloaded
the app works with no network connection at all.

- **Read a document, follow along.** The sentence being played is marked on
  screen. The mark is drawn as an outline around the letters rather than as a
  heavier weight, so the text never shifts under you while you read.
- **Playback starts before generation finishes.** The first sentence is spoken
  as soon as it is ready, and the rest follows.
- **It stays out of the way.** You choose how much processor and memory the app
  may use, and it stays inside that while you work on something else.
- **Automations can use it too.** A local HTTP service on the loopback address,
  and an MCP server for tools that speak that protocol. Both are the same
  engine, the same limits, and the same one job at a time.

Windows 11 and macOS 14 (Apple Silicon). No Python, Node, or database server
to install.

## Running it from a checkout

```
uv sync
uv run python -m echoact
```

The first launch offers to download the speech model (about 385 MB) and shows
its licence terms before it does. Generation works offline afterwards.

## Local service

The service is on by default and listens only on `127.0.0.1:8765`. It always
requires a credential; one is created for you on first launch and is shown in
the app under Settings. It cannot be reached from another machine, and there
is no way to configure it to be.

Turn it off in Settings if you do not want it. The app, generation, playback
and the library all work exactly the same with it off.

| | |
| --- | --- |
| Base | `http://127.0.0.1:8765/api/v1` |
| Auth | `Authorization: Bearer <credential>` |
| Discovery | `GET /status`, `GET /models` |
| Before committing | `POST /estimate` — validates, counts segments, estimates length, says whether the slot is free |
| Generate | `POST /jobs` — needs a duplicate-prevention key; may ask to wait up to 10 s |
| Follow | `GET /jobs/{id}`, `GET /jobs/{id}/segments`, `POST /jobs/{id}/cancel` |
| Collect | `GET /jobs/{id}/audio`, `GET /jobs/{id}/result` |

One generation runs at a time across the app and every client, so a second
request is refused with a retry-after hint rather than queued.

## MCP

Off by default; enable it in Settings. The MCP server is a small process your
MCP client starts, which talks to the local service over loopback with a
credential you issue for it. It adds nothing the HTTP service does not already
do, and its permissions are exactly that credential's.

Tools: `list_models`, `estimate_speech`, `create_speech`, `get_speech_job`,
`cancel_speech_job`, `list_speech_segments`, `get_speech_result`,
`list_speech_history`.

## Your data

Documents and generation history are saved only when you ask. Results from a
one-off job are cleaned up when they are replaced or when the app exits, and a
result produced for an integration stays retrievable for an hour. Logs contain
no text and no audio. Nothing is uploaded, and there is no telemetry.

## Repository

```
docs/design.md    The requirements baseline. Every behaviour traces to an
                  identifier in it; Appendix A records what was measured.
echoact/          The application.
spikes/           Standalone measurements that settled open questions.
tests/            pytest. `-m engine` needs the model; the rest does not.
```

`CLAUDE.md` is the contributor's short version.

## Licence

The application code is MIT (see `LICENSE`). The speech model is downloaded
separately and carries its own licence with use restrictions, which the app
presents for acceptance before it prepares the model for the first time. Being
able to run it locally is not a right to redistribute it.
