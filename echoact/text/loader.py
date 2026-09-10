"""Turning a chosen file, or an uploaded body, into the text of an input box.

This is the one place F-02's "open a text file" actually happens, and it is
shared: F-37 requires the GUI, REST and MCP to apply the same limits and the
same support policy, so the REST upload path calls ``load_bytes`` and the
file dialog calls ``load_file``, and neither has rules of its own.

Three requirements shape the order of operations here.

* F-03's 2,000,000-byte ceiling is applied to the file's size *before* the
  bytes are read, not to what was read, so an oversized file is never pulled
  into memory to be measured.  The read is still bounded, because a file can
  grow between the two calls.
* F-32 says that on any failure the caller's input, documents and playback
  job are unchanged.  Nothing in this module writes anything, mutates any
  argument, or touches application state; it reads and returns, and every
  refusal is an exception raised before a value exists.  That is why the size
  and support checks live here rather than in the widget that will replace
  its own contents.
* F-36 forbids getting under the 50,000 code-point limit by truncating.  The
  limit is therefore a rejection with the real count attached, never a slice.

Newlines are normalised to ``\\n`` because a CRLF file would otherwise count
its line breaks twice against F-03's limit and make every 4.2 code-point
offset disagree with what the reader sees.  The substitution is recorded on
the result, since it is the one respect in which the returned text is not
byte-for-byte what the file held.
"""

from __future__ import annotations

import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..errors import Code, EchoActError, Problem
from ..policy import MAX_IMPORT_FILE_BYTES, MAX_INPUT_CODEPOINTS
from .sniff import (
    SELECTABLE_ENCODINGS,
    SUPPORTED_FORMATS,
    Confidence,
    FileKind,
    Verdict,
    bom_encoding,
    sniff,
    verdict_for_text,
)

#: Encodings ``load_file`` will honour when one is passed explicitly.  The
#: selector's own list plus the forms a byte-order mark implies, and nothing
#: else: an encoding that cannot fail would let F-34's "never silently
#: corrupted" be defeated by asking for it.
ALLOWED_ENCODINGS: Final = frozenset(SELECTABLE_ENCODINGS) | {
    "utf-8-sig",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
}

_REMEDIES_TOO_LARGE: Final = (
    "Split the text into files of at most 2,000,000 bytes each and open them one at a time.",
    "Or paste just the part you want spoken.",
)
_REMEDIES_TOO_LONG: Final = (
    "Split the text into parts of at most 50,000 characters and generate them one at a time.",
    "Or shorten the text; EchoAct will not cut it for you, so that nothing is lost silently.",
)
_REMEDIES_EMPTY: Final = (
    "Choose a file that contains text.",
    "Or type the text you want spoken directly into EchoAct.",
)
_REMEDIES_CONFIRM: Final = (
    "Check the preview and confirm that this file really is the text you want read.",
    "Or save the content as a plain-text file (.txt) encoded in UTF-8 and open that file.",
)
_REMEDIES_MISSING: Final = (
    "Check that the file still exists at that location, then choose it again.",
    "Or type the text you want spoken directly into EchoAct.",
)
_REMEDIES_PERMISSION: Final = (
    "Grant your account permission to read the file, or copy it somewhere you can read.",
    "Or open the file yourself and paste the text into EchoAct.",
)
_REMEDIES_UNREADABLE = _REMEDIES_MISSING

#: Refusals a confirmed encoding may overturn; see ``_resolve_text``.
_OVERRIDABLE_CODES: Final = frozenset({Code.FILE_ENCODING, Code.FILE_NOT_TEXT})


@dataclass(frozen=True, slots=True)
class LoadedText:
    """What a successful open produced, and what was done to it.

    ``text`` is the job's source text as F-29 means it: everything the file
    contained, in the order it contained it, with no truncation and no
    replacement characters.  The remaining fields exist so a surface can be
    honest about the one edit that was made -- the line endings -- because a
    user counting characters, or a caller counting the 4.2 code-point offsets
    it will later be handed, would otherwise be counting something else.
    """

    text: str
    encoding: str
    kind: FileKind
    confidence: Confidence
    source_name: str
    byte_size: int
    line_endings_normalised: bool
    crlf_count: int
    cr_count: int
    had_bom: bool
    #: True when the content needed F-35's confirmation and the caller gave it.
    confirmed_as_text: bool

    @property
    def codepoints(self) -> int:
        """The count F-03 displays and limits.  Code points, per 4.2, and of
        the normalised text, which is what the reader will see."""
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        """A description with no body text in it, for logs and diagnostics
        (N-20).  The file's name is included because the user chose it and
        the GUI shows it; no directory ever appears here."""
        return {
            "source_name": self.source_name,
            "kind": self.kind.value,
            "encoding": self.encoding,
            "confidence": self.confidence.value,
            "byte_size": self.byte_size,
            "codepoints": self.codepoints,
            "line_endings_normalised": self.line_endings_normalised,
            "had_bom": self.had_bom,
            "confirmed_as_text": self.confirmed_as_text,
        }


def inspect_file(path: str | Path) -> Verdict:
    """What would happen if this file were opened, without opening it into
    anything.

    The GUI calls this to build F-34's encoding selector and F-35's preview
    before it disturbs the input box, which is how F-36's "if validation
    fails the existing content is retained" costs nothing to honour.
    """
    return _probe(Path(path))[1]


def inspect_bytes(data: bytes, *, filename: str | None = None) -> Verdict:
    """``inspect_file`` for a body that is already in memory, such as an
    upload (F-37).  The size limit is the same one, since 4.1 caps the
    request body at the same 2,000,000 bytes."""
    if len(data) > MAX_IMPORT_FILE_BYTES:
        return _too_large_verdict(len(data))
    return sniff(data, filename=filename)


def load_file(
    path: str | Path,
    *,
    encoding: str | None = None,
    confirm_unknown: bool = False,
) -> LoadedText:
    """Read a file as the text to be spoken (F-02, F-03, F-32 to F-36).

    ``encoding`` is F-34's confirmed choice and is honoured only for a file
    whose encoding could not be determined; it can never overrule *what the
    file is*, so naming an encoding is not a way to have a PNG read aloud.
    ``confirm_unknown`` is F-35's confirmation, and it is the caller's to
    give only after the preview in the verdict has been shown.

    Raises ``EchoActError`` for every rejection, with F-33's remedies in the
    error's detail.  Nothing is read, changed, or kept when it does.
    """
    file_path = Path(path)
    data, verdict = _probe(file_path)
    if data is None:
        raise verdict.as_error()
    return _build(
        data,
        verdict,
        filename=file_path.name,
        encoding=encoding,
        confirm_unknown=confirm_unknown,
    )


def load_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    encoding: str | None = None,
    confirm_unknown: bool = False,
) -> LoadedText:
    """The same read for a body already in memory (F-37).

    An automated caller reaches the identical checks in the identical order.
    In particular it is never given an encoding by default: if the bytes do
    not settle the question, this raises ``FILE_ENCODING`` carrying the
    candidates, and the caller either re-sends with ``encoding=`` or converts
    the file, which is what F-37 means by a correctable error.
    """
    if len(data) > MAX_IMPORT_FILE_BYTES:
        raise _too_large_verdict(len(data)).as_error()
    verdict = sniff(data, filename=filename)
    return _build(
        data,
        verdict,
        filename=filename,
        encoding=encoding,
        confirm_unknown=confirm_unknown,
    )


def normalise_newlines(text: str) -> tuple[str, int, int]:
    """Fold CRLF and lone CR to ``\\n``, reporting how many of each there were.

    Returned rather than hidden because it moves every offset after the first
    line break, and 4.2's segment ranges are offsets into exactly this text.
    """
    crlf_count = text.count("\r\n")
    cr_count = text.count("\r") - crlf_count
    if crlf_count or cr_count:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, crlf_count, cr_count


# ------------------------------------------------------------- internals ---


def _problem_error(
    code: Code, message: str, remedies: tuple[str, ...], **detail: Any
) -> EchoActError:
    problem = Problem(code=code, message=message, remedies=remedies)
    return EchoActError(
        problem.code,
        problem.message,
        detail={
            "remedies": list(problem.remedies),
            "supported_formats": list(SUPPORTED_FORMATS),
            **detail,
        },
    )


def _fs_verdict(code: Code, message: str, remedies: tuple[str, ...]) -> Verdict:
    return Verdict(
        kind=FileKind.UNKNOWN,
        readable_as_text=False,
        problem=Problem(code=code, message=message, remedies=remedies),
    )


def _too_large_verdict(size: int) -> Verdict:
    return Verdict(
        kind=FileKind.UNKNOWN,
        readable_as_text=False,
        problem=Problem(
            code=Code.FILE_TOO_LARGE,
            message=(
                f"This file is {size:,} bytes, and EchoAct opens files up to "
                f"{MAX_IMPORT_FILE_BYTES:,} bytes."
            ),
            remedies=_REMEDIES_TOO_LARGE,
        ),
        byte_size=size,
        detail={"byte_size": size, "limit_bytes": MAX_IMPORT_FILE_BYTES},
    )


def _probe(path: Path) -> tuple[bytes | None, Verdict]:
    """Stat, gate on size, read, and sniff -- in that order (F-03, F-32).

    Returns ``(None, verdict)`` when nothing was read.  The read is capped one
    byte above the limit so that a file which grew between the stat and the
    open is still refused rather than admitted at whatever size it reached.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, _fs_verdict(
            Code.FILE_NOT_FOUND, "That file no longer exists.", _REMEDIES_MISSING
        )
    except PermissionError:
        return None, _fs_verdict(
            Code.FILE_PERMISSION,
            "That file cannot be read with your current permissions.",
            _REMEDIES_PERMISSION,
        )
    except OSError as exc:
        return None, _fs_verdict(
            Code.FILE_CORRUPT,
            f"That file could not be read from the storage device ({exc.strerror or 'I/O error'}).",
            _REMEDIES_UNREADABLE,
        )

    if stat_module.S_ISDIR(info.st_mode):
        return None, _fs_verdict(
            Code.FILE_UNSUPPORTED, "That is a folder, not a file.", _REMEDIES_MISSING
        )
    if info.st_size > MAX_IMPORT_FILE_BYTES:
        return None, _too_large_verdict(info.st_size)

    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_IMPORT_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, _fs_verdict(
            Code.FILE_NOT_FOUND, "That file no longer exists.", _REMEDIES_MISSING
        )
    except PermissionError:
        return None, _fs_verdict(
            Code.FILE_PERMISSION,
            "That file cannot be read with your current permissions.",
            _REMEDIES_PERMISSION,
        )
    except OSError as exc:
        return None, _fs_verdict(
            Code.FILE_CORRUPT,
            f"That file could not be read from the storage device ({exc.strerror or 'I/O error'}).",
            _REMEDIES_UNREADABLE,
        )

    if len(data) > MAX_IMPORT_FILE_BYTES:
        return None, _too_large_verdict(len(data))
    return data, sniff(data, filename=path.name)


def _normalise_encoding_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _decode_strictly(data: bytes, encoding: str) -> str:
    """Decode or refuse.  There is no third outcome: F-34 forbids handing
    back text repaired with replacement characters, so no lossy decode
    happens on this path -- only in a preview, which is labelled as one."""
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise _problem_error(
            Code.FILE_ENCODING,
            f"The file is not valid {encoding.upper()}: the bytes at position {exc.start:,} "
            "cannot be read that way.",
            (
                "Check the preview and choose a different encoding.",
                "Or re-save the file as UTF-8 in a text editor and open it again.",
            ),
            encoding=encoding,
            failure_offset=exc.start,
            failure_reason=exc.reason,
            selectable_encodings=list(SELECTABLE_ENCODINGS),
        ) from exc


def _resolve_text(
    data: bytes,
    verdict: Verdict,
    *,
    filename: str | None,
    encoding: str | None,
) -> tuple[str, Verdict]:
    """Produce the decoded text and the verdict that governs confirmation.

    An explicitly chosen encoding answers one question only -- how to read
    the bytes -- so it is accepted only where that was the question.  A file
    refused for *what it is*, such as a PDF, an archive or an encrypted
    document, stays refused however it is asked for, which is what keeps
    F-35's "not force-interpreted as text" from having a parameter that
    switches it off.  The two codes it may override are the two the sniffer
    reaches by judgement rather than by a signature: an undecidable encoding,
    and text that looked binary -- which is exactly what a UTF-16 file with
    no byte-order mark looks like.  Both are then re-judged on the decoded
    result, so choosing an encoding can reveal text but never invent it.
    """
    if encoding is None:
        if verdict.problem is not None:
            raise verdict.as_error()
        assert verdict.encoding is not None  # an ok verdict always names one
        return _decode_strictly(data, verdict.encoding), verdict

    chosen = _normalise_encoding_name(encoding)
    if chosen not in ALLOWED_ENCODINGS:
        raise _problem_error(
            Code.FILE_ENCODING,
            f"EchoAct cannot read files as {encoding!r}.",
            (
                "Choose one of the encodings EchoAct offers.",
                "Or re-save the file as UTF-8 in a text editor and open it again.",
            ),
            encoding=encoding,
            selectable_encodings=list(SELECTABLE_ENCODINGS),
        )
    if verdict.problem is not None and verdict.problem.code not in _OVERRIDABLE_CODES:
        raise verdict.as_error()

    text = _decode_strictly(data, chosen)
    rechecked = verdict_for_text(
        text,
        chosen,
        byte_size=len(data),
        filename=filename,
        confidence=Confidence.CERTAIN,
    )
    if rechecked.problem is not None:
        raise rechecked.as_error()
    return text, rechecked


def _build(
    data: bytes,
    verdict: Verdict,
    *,
    filename: str | None,
    encoding: str | None,
    confirm_unknown: bool,
) -> LoadedText:
    text, governing = _resolve_text(data, verdict, filename=filename, encoding=encoding)

    if governing.needs_confirmation and not confirm_unknown:
        # F-35.  The name gives no assurance, so the content is offered as a
        # preview and the answer comes back from a person -- or, for F-37's
        # non-interactive caller, as an explicit acknowledgement on the next
        # call.  Either way it is corrected by the caller, not assumed here.
        raise _problem_error(
            Code.FILE_UNSUPPORTED,
            "This file is not a recognised text file, though its content does read as text. "
            "Confirm that you want it opened as plain text.",
            _REMEDIES_CONFIRM,
            kind=governing.kind.value,
            needs_confirmation=True,
            encoding=governing.encoding,
            preview=governing.preview,
            extension_mismatch=bool(governing.detail.get("extension_mismatch")),
        )

    text, crlf_count, cr_count = normalise_newlines(text)

    if not text.strip():
        raise _problem_error(
            Code.INPUT_EMPTY,
            "That file contains no text, only blank space.",
            _REMEDIES_EMPTY,
            codepoints=len(text),
        )
    if len(text) > MAX_INPUT_CODEPOINTS:
        # F-36: report the real count and stop.  Truncating here would be the
        # one failure mode the user cannot see, since the text that vanished
        # is the text they were not looking at.
        raise _problem_error(
            Code.INPUT_TOO_LONG,
            f"That file holds {len(text):,} characters, and the limit is "
            f"{MAX_INPUT_CODEPOINTS:,}.",
            _REMEDIES_TOO_LONG,
            codepoints=len(text),
            limit_codepoints=MAX_INPUT_CODEPOINTS,
        )

    assert governing.encoding is not None
    return LoadedText(
        text=text,
        encoding=governing.encoding,
        kind=governing.kind,
        confidence=governing.confidence,
        source_name=filename or "",
        byte_size=len(data),
        line_endings_normalised=bool(crlf_count or cr_count),
        crlf_count=crlf_count,
        cr_count=cr_count,
        had_bom=bom_encoding(data) is not None,
        confirmed_as_text=governing.needs_confirmation,
    )


__all__ = [
    "ALLOWED_ENCODINGS",
    "LoadedText",
    "inspect_bytes",
    "inspect_file",
    "load_bytes",
    "load_file",
    "normalise_newlines",
]
