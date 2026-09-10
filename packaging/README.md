# Building and signing a distribution

N-10 fixes the shape: Windows is a folder containing the executable and its
companion files; macOS is an app. The user must not need to install Python,
Node, or a database server. N-29 fixes the trust: the origin and integrity of
official files must be verifiable, Windows builds are code-signed, macOS builds
are signed and notarized, and installing must never require the user to turn a
security feature off.

## Build

```
uv sync
uv run python scripts/fetch_fonts.py        # optional, see below
uv run pyinstaller packaging/echoact.spec --noconfirm
```

The result is `dist/EchoAct/` on Windows and `dist/EchoAct.app` on macOS.

Two things are deliberately **not** in it:

**The model weights.** F-09 makes preparation an explicit user action, N-11 says
being able to run a model locally is not a right to redistribute it, and F-84
resolves every download against the manifest at run time. The app downloads the
model on first use, after showing its licence terms for acceptance.

**The typeface, unless you fetched it.** `scripts/fetch_fonts.py` acquires
Pretendard under the SIL Open Font License and puts its licence beside it. The
app runs without it; what it loses is A.2's guarantee that a layout measurement
taken on Windows means the same thing on macOS, which matters for N-12 and N-30
results and not for whether it works. Fetch it for a release build.

One executable, three jobs. A PyInstaller bundle cannot run
`python -m echoact.engine.worker`, because `sys.executable` is the frozen binary
and `-m` means nothing to it. So the binary re-invokes itself: `--worker` for the
synthesis child A.2's process layout needs, `--mcp` for the stdio MCP server an
MCP client starts. Neither flag is a public interface.

## What the build was checked against

A Windows directory build was produced and exercised, so the layout above is
what came out rather than what was intended:

- `dist/EchoAct/` is 230 MB before any model, with Qt as separate DLLs under
  `_internal/PySide6/` — which is the arrangement LGPL relinking needs.
- The executable re-invokes itself correctly. `EchoAct.exe --mcp` reports a
  missing credential and exits, and the real supervisor drove
  `EchoAct.exe --worker` through a load and a synthesis: the frozen child
  loaded the model in 0.86 s at 44,100 Hz with ten voices, produced 4.74
  seconds of audio in 1.47, reported 495 MiB through the Job Object, and was
  released in 0.09 s. F-87's provider allow-list held inside the bundle, which
  is the check worth having: the runtime is packaged differently there.
- The application starts, creates its data tree, and takes the single-instance
  lock (F-85).
- It logs that no typeface is bundled, so a measurement taken from this build
  is machine-specific. Fetch the font for a release build.

Not checked, because neither certificate exists yet: signing, notarization, and
installation on a machine that has never seen the build.

## Windows signing

Needs an EV or OV code-signing certificate on a hardware token or in a cloud
signing service. **This has not been procured** — A.4 lists it as a release
blocker with procurement lead time.

Sign the executable and every DLL the build produced, not only the executable:
a signed launcher next to unsigned libraries tells a user nothing about what
actually runs.

```
$files = Get-ChildItem dist\EchoAct -Recurse -Include *.exe,*.dll,*.pyd
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /n "<subject name>" $files
signtool verify /pa /all dist\EchoAct\EchoAct.exe
```

Timestamping is not optional: without it every signature stops verifying the day
the certificate expires, and a distribution that becomes untrusted on a date
nobody remembers is not one whose integrity is verifiable.

Publish a SHA-256 for the packaged archive alongside it, so the download can be
checked independently of the signature.

## macOS signing and notarization

Needs an Apple Developer ID Application certificate and an app-specific password
or API key. **Also not procured.**

```
codesign --deep --force --options runtime --timestamp \
  --sign "Developer ID Application: <name> (<team id>)" dist/EchoAct.app
ditto -c -k --keepParent dist/EchoAct.app dist/EchoAct.zip
xcrun notarytool submit dist/EchoAct.zip --keychain-profile "<profile>" --wait
xcrun stapler staple dist/EchoAct.app
spctl --assess --type execute --verbose dist/EchoAct.app
```

`--options runtime` enables the hardened runtime, which notarization requires.
Staple the ticket: a notarized app that is not stapled fails to launch on a
machine that is offline, and F-79's "no internet needed to generate" would be
false at the first hurdle.

## LGPL

PySide6 and Qt are LGPLv3. The directory build keeps Qt as separate shared
libraries, which is what makes relinking possible; a one-file build would
statically bundle them and would not. Ship the LGPL text and a note saying which
libraries it covers and where their sources are — N-11 makes honouring a licence
a condition rather than a courtesy.

## Before calling a build releasable

Section 8.3's conditions, in the order they are cheapest to check:

- [ ] Launch, normal exit, and forced-termination recovery on each OS
- [ ] Offline generation with a prepared model (A-23)
- [ ] Library restore from a backup made by the previous version
- [ ] REST and MCP result retrieval from the reviewed clients (F-62)
- [ ] The model's licence shown and accepted before first preparation (N-11)
- [ ] Signature and notarization verified on a machine that has never seen the
      build, by a user who is not an administrator
- [ ] **Voice quality reviewed by a person**, in both languages, against
      Section 8.2's sample. `spikes/voice_review.py` renders it and reports what
      a machine can detect — silence, clipping, implausible durations — which is
      not what N-08 asks for. Missing speech, unnecessary repetition, corrupted
      audio and clearly wrong sentences are release-blocking, and no test in this
      repository can find them.
