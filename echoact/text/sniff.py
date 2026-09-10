"""Whether a file may be read as text at all, and in which encoding.

F-32 forbids trusting the extension, so every decision here is taken from the
bytes; a name, when one is known, only decides whether F-35 wants the user to
confirm before the content is accepted.  F-33 forbids two things this module
therefore does not contain: any suggestion that renaming the file will help,
and any automatic upload or online conversion.  There is deliberately no
network import anywhere in this module, and ``tests/test_sniff.py`` asserts
that rather than trusting review to notice one being added later.

F-34 fixes the encoding order.  UTF-8 is tried first and CP949 only as a
fallback, because the asymmetry runs one way: CP949 text is rarely valid
UTF-8 by accident, while UTF-8 text is routinely valid CP949.  When neither
is confident the file is *not* decoded with ``errors="replace"`` and handed
on -- F-34 forbids exactly that -- the verdict carries the candidate list and
a preview instead, so a person can choose (F-34) or an automated caller
receives a correctable error (F-37).

The module is pure: it opens nothing, writes nothing, and keeps no state.
``echoact.text.loader`` owns the filesystem.  That split is what makes F-32's
"on failure the existing input, documents, and playback job are unchanged"
true by construction rather than by discipline.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from ..errors import Code, EchoActError, Problem

#: Bytes the structural checks look at.  Magic numbers sit in the first few
#: dozen; control-character density is sampled rather than counted over the
#: whole file, because a file whose first 8 KiB are clean prose is prose and
#: the cost of deciding then stays flat at the 2,000,000-byte import ceiling.
SNIFF_WINDOW_BYTES: Final = 8192

#: Code points shown to the user so an encoding can be judged (F-34, F-35).
PREVIEW_CODEPOINTS: Final = 400

#: Share of C0 control characters above which a window is not prose.  Real
#: text carries tab, newline, carriage return, form feed and vertical tab and
#: essentially nothing else; a handful of stray escapes in a long log must
#: not condemn it, hence the absolute floor as well.
MAX_CONTROL_RATIO: Final = 0.05
MIN_CONTROL_COUNT: Final = 3

#: What F-02 supports, named the way F-33 requires the notice to name it.
SUPPORTED_FORMATS: Final = ("TXT", "Markdown")

#: F-34's automatic pair, in the order they are attempted.
AUTO_ENCODINGS: Final = ("utf-8", "cp949")

#: What the encoding selector may offer once automatic reading has failed.
#: Deliberately excludes any encoding that cannot fail -- latin-1 decodes
#: every byte sequence, so offering it would be offering mojibake with no
#: signal that anything went wrong, which is F-34's silent corruption under
#: another name.
SELECTABLE_ENCODINGS: Final = ("utf-8", "cp949", "utf-16", "utf-16-le", "utf-16-be")

ENCODING_LABELS: Final = {
    "utf-8": "UTF-8",
    "utf-8-sig": "UTF-8 with byte-order mark",
    "cp949": "CP949 (Korean, Windows ANSI)",
    "utf-16": "UTF-16",
    "utf-16-le": "UTF-16 little-endian",
    "utf-16-be": "UTF-16 big-endian",
    "utf-32": "UTF-32",
    "utf-32-le": "UTF-32 little-endian",
    "utf-32-be": "UTF-32 big-endian",
}

# Remedy sentences.  F-33 requires the reason, the supported formats, and how
# to copy the text in or convert to TXT.  It also forbids suggesting that a
# rename fixes anything and forbids offering an online conversion, so no
# string here may mention either; the tests scan these constants for that.
_COPY_IN: Final = (
    "Open the file in an application that can display it, select the text, and paste it "
    "into EchoAct."
)
_SAVE_AS_TXT: Final = (
    "Save or export the content as a plain-text file (.txt) encoded in UTF-8, then open that file."
)
_TYPE_INSTEAD: Final = "Type or paste the text you want spoken directly into EchoAct."

_REMEDIES_DOCUMENT: Final = (_COPY_IN, _SAVE_AS_TXT)
_REMEDIES_IMAGE: Final = (
    "EchoAct does not read text inside pictures; it has no text recognition.",
    _TYPE_INSTEAD,
)
_REMEDIES_MEDIA: Final = (
    "EchoAct does not transcribe audio or video.",
    _TYPE_INSTEAD,
)
_REMEDIES_ARCHIVE: Final = (
    "Unpack the archive with your own tool, then open a text file from it.",
    _TYPE_INSTEAD,
)
_REMEDIES_EXECUTABLE: Final = (
    "EchoAct never runs a file you open, and it will not read a program as text.",
    _TYPE_INSTEAD,
)
_REMEDIES_ENCRYPTED: Final = (
    "Open it in the application that created it, supply the password, and save an unprotected "
    "plain-text copy.",
    _COPY_IN,
)
_REMEDIES_CORRUPT: Final = (
    "Try another copy of the file, or the original it was made from.",
    _SAVE_AS_TXT,
)
_REMEDIES_BINARY: Final = (
    "Choose a plain-text file (.txt) or a Markdown file (.md).",
    _TYPE_INSTEAD,
)
_REMEDIES_ENCODING: Final = (
    "Check the preview, then choose the encoding the file was written in.",
    "Or re-save the file as UTF-8 in a text editor and open it again.",
)
_REMEDIES_EMPTY: Final = (
    "Choose a file that contains text.",
    _TYPE_INSTEAD,
)


class FileKind(StrEnum):
    """What the bytes turned out to be.  ``UNKNOWN`` covers a file the module
    never got to look at, such as one refused on size or on permissions."""

    TEXT = "text"
    EMPTY = "empty"
    HTML = "html"
    XML = "xml"
    RTF = "rtf"
    PDF = "pdf"
    DOC = "doc"
    DOCX = "docx"
    XLS = "xls"
    XLSX = "xlsx"
    PPT = "ppt"
    PPTX = "pptx"
    HWP = "hwp"
    HWPX = "hwpx"
    EPUB = "epub"
    OPENDOCUMENT = "opendocument"
    OLE_DOCUMENT = "ole_document"
    ARCHIVE = "archive"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    EXECUTABLE = "executable"
    DATABASE = "database"
    BINARY = "binary"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """How firmly the encoding was established.

    ``CERTAIN`` means the file said so itself with a byte-order mark, or the
    bytes admit only one reading.  ``LIKELY`` means one candidate decoded
    strictly and the others did not.  ``UNCERTAIN`` means nothing decoded,
    which F-34 turns into a question rather than into a guess.
    """

    CERTAIN = "certain"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class EncodingCandidate:
    """One entry in F-34's encoding selector.

    The preview of a candidate that does not decode cleanly is produced with
    replacement characters *on purpose*: seeing the damage is how a person
    rules the choice out.  It is never a source of loaded text -- the loader
    decodes strictly and only strictly.
    """

    name: str
    label: str
    decodes_cleanly: bool
    preview: str
    failure_offset: int | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "encoding": self.name,
            "label": self.label,
            "decodes_cleanly": self.decodes_cleanly,
            "failure_offset": self.failure_offset,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """The answer to "may this be read as text, and how".

    One object carries every case F-32 requires to be distinguished --
    unsupported, corrupt, encrypted, permission-denied, oversized, encoding
    error -- because a caller that must distinguish them should not have to
    catch a different exception for each.  ``problem`` is ``None`` exactly
    when the bytes may become text.
    """

    kind: FileKind
    readable_as_text: bool
    encoding: str | None = None
    confidence: Confidence = Confidence.UNCERTAIN
    candidates: tuple[EncodingCandidate, ...] = ()
    problem: Problem | None = None
    #: F-35: an unknown or absent extension means the user sees the preview
    #: and says yes before the text is accepted.
    needs_confirmation: bool = False
    preview: str = ""
    #: True when ``preview`` contains replacement characters and therefore
    #: shows damage rather than content.
    preview_lossy: bool = False
    byte_size: int = 0
    #: The specific format behind a family kind, e.g. "png" inside IMAGE.
    format_name: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.problem is None and self.readable_as_text

    def to_dict(self) -> dict[str, Any]:
        """What a surface may show or send.  Carries no path and no filename:
        F-57 keeps internal paths out of error payloads and N-20 keeps them
        out of logs, and this object reaches both."""
        body: dict[str, Any] = {
            "kind": self.kind.value,
            "format": self.format_name,
            "readable_as_text": self.readable_as_text,
            "encoding": self.encoding,
            "confidence": self.confidence.value,
            "needs_confirmation": self.needs_confirmation,
            "byte_size": self.byte_size,
            "supported_formats": list(SUPPORTED_FORMATS),
        }
        if self.candidates:
            body["encoding_candidates"] = [c.to_dict() for c in self.candidates]
        if self.problem is not None:
            body["code"] = self.problem.code.value
            body["message"] = self.problem.message
            body["remedies"] = list(self.problem.remedies)
        if self.detail:
            body["detail"] = dict(self.detail)
        return body

    def as_error(self) -> EchoActError:
        """The rejection as the one exception type that crosses a boundary.

        F-33's remedies travel in ``detail`` so REST and MCP deliver the same
        guidance the GUI shows; N-24 wants one set of error semantics, not a
        richer one for the surface that happens to be in-process.
        """
        if self.problem is None:
            raise AssertionError("as_error() on a verdict with no problem")
        detail: dict[str, Any] = {
            "kind": self.kind.value,
            "remedies": list(self.problem.remedies),
            "supported_formats": list(SUPPORTED_FORMATS),
        }
        if self.format_name:
            detail["format"] = self.format_name
        if self.candidates:
            detail["encoding_candidates"] = [c.to_dict() for c in self.candidates]
        if self.needs_confirmation:
            detail["needs_confirmation"] = True
        detail.update(self.detail)
        return EchoActError(self.problem.code, self.problem.message, detail=detail)


# --------------------------------------------------------------- helpers ---

_OLE_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Extensions F-02 names.  A name outside this set is not a refusal: it only
#: means F-35's confirmation is required, because the content still decides.
TEXT_EXTENSIONS: Final = frozenset({".txt", ".md", ".markdown", ".mkd", ".mdown", ".text"})

#: Names that claim to be something this app cannot read.  Used only to say
#: "the name and the content disagree" in a verdict's detail; the bytes are
#: what decide, per F-32.
_NON_TEXT_EXTENSIONS: Final = frozenset(
    {
        ".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".epub", ".odt", ".rtf",
        ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".gz", ".tar",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
        ".wav", ".mp3", ".mp4", ".m4a", ".mov", ".ogg", ".flac", ".avi", ".mkv",
        ".exe", ".dll", ".so", ".dylib", ".bin", ".db", ".sqlite", ".sqlite3",
    }
)

_HTML_STARTS: Final = ("<!doctype html", "<html", "<head", "<body")
_PDF_ENCRYPT_RE: Final = re.compile(rb"/Encrypt\s*\d+\s+\d+\s+R")


def _u16(name: str) -> bytes:
    """A compound-file stream name as it appears in the raw bytes.

    Directory entries in an OLE compound file store their names in UTF-16LE,
    so searching for the ASCII spelling finds nothing.  Reading the directory
    properly would mean parsing the allocation tables of a format the app
    refuses either way.
    """
    return name.encode("utf-16-le")


def _extension(filename: str | None) -> str:
    if not filename:
        return ""
    _, dot, ext = filename.rpartition(".")
    return f".{ext.lower()}" if dot and ext else ""


def _count_control_bytes(sample: bytes) -> int:
    printable_controls = (0x09, 0x0A, 0x0B, 0x0C, 0x0D)
    controls = sum(1 for b in sample if b < 0x20 and b not in printable_controls)
    return controls + sample.count(0x7F)


def _byte_window_is_control_dense(sample: bytes) -> bool:
    if not sample:
        return False
    controls = _count_control_bytes(sample)
    return controls >= MIN_CONTROL_COUNT and controls / len(sample) > MAX_CONTROL_RATIO


def _text_is_control_dense(text: str) -> bool:
    if not text:
        return False
    controls = sum(
        1
        for ch in text
        if ch == "\ufffd" or (unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\v\f")
    )
    return controls >= MIN_CONTROL_COUNT and controls / len(text) > MAX_CONTROL_RATIO


def _preview_of(text: str) -> str:
    return text[:PREVIEW_CODEPOINTS]


def _problem_verdict(
    kind: FileKind,
    code: Code,
    message: str,
    remedies: tuple[str, ...],
    *,
    size: int,
    format_name: str = "",
    detail: dict[str, Any] | None = None,
    needs_confirmation: bool = False,
    candidates: tuple[EncodingCandidate, ...] = (),
    preview: str = "",
    preview_lossy: bool = False,
) -> Verdict:
    return Verdict(
        kind=kind,
        readable_as_text=False,
        problem=Problem(code=code, message=message, remedies=remedies),
        byte_size=size,
        format_name=format_name,
        detail=detail or {},
        needs_confirmation=needs_confirmation,
        candidates=candidates,
        preview=preview,
        preview_lossy=preview_lossy,
    )


def _unsupported(
    kind: FileKind, format_name: str, size: int, remedies: tuple[str, ...], reason: str
) -> Verdict:
    return _problem_verdict(
        kind, Code.FILE_UNSUPPORTED, reason, remedies, size=size, format_name=format_name
    )


# ------------------------------------------------------- format families ---


def _image_format(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    # "BM" is two printable letters, so a bitmap has to prove itself or a
    # sentence beginning "BMW..." becomes an image: the reserved words must
    # be zero and either the declared size or the pixel offset must fit.
    if data.startswith(b"BM") and len(data) >= 14 and data[6:10] == b"\x00\x00\x00\x00":
        declared = int.from_bytes(data[2:6], "little")
        pixel_offset = int.from_bytes(data[10:14], "little")
        if declared == len(data) or 0 < pixel_offset <= len(data):
            return "bmp"
    return ""


def _media_format(data: bytes) -> tuple[str, FileKind]:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav", FileKind.AUDIO
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "avi", FileKind.VIDEO
    if data.startswith(b"ID3"):
        return "mp3", FileKind.AUDIO
    if data.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "mp3", FileKind.AUDIO
    if data.startswith(b"OggS"):
        return "ogg", FileKind.AUDIO
    if data.startswith(b"fLaC"):
        return "flac", FileKind.AUDIO
    if data[4:8] == b"ftyp":
        brand = data[8:12].decode("ascii", "replace").strip()
        kind = FileKind.AUDIO if brand in {"M4A", "M4B", "M4P"} else FileKind.VIDEO
        return (f"iso-bmff ({brand})" if brand else "iso-bmff"), kind
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "matroska", FileKind.VIDEO
    return "", FileKind.BINARY


def _archive_format(data: bytes) -> str:
    if data.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if data.startswith(b"\x1f\x8b"):
        return "gzip"
    if data.startswith(b"BZh") and data[3:4].isdigit():
        return "bzip2"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstandard"
    if data[257:262] == b"ustar":
        return "tar"
    return ""


def _executable_format(data: bytes) -> str:
    if data.startswith(b"\x7fELF"):
        return "elf"
    if data.startswith(
        (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe")
    ):
        return "mach-o"
    if data.startswith(b"\xca\xfe\xba\xbe"):
        # Shared by a Mach-O universal binary and a Java class file; either
        # way it is a program, and the notice says the same of both.
        return "mach-o universal or java class"
    if data.startswith(b"MZ"):
        if len(data) >= 0x40:
            pe_offset = int.from_bytes(data[0x3C:0x40], "little")
            if 0 < pe_offset < len(data) - 4 and data[pe_offset : pe_offset + 4] == b"PE\x00\x00":
                return "windows pe"
        return "dos executable"
    return ""


def _pdf_verdict(data: bytes, size: int) -> Verdict:
    """PDF, and which of F-32's cases it is.

    Encryption is read from the trailer's ``/Encrypt`` reference rather than
    from any attempt to open the document, because the distinction F-32 wants
    -- encrypted as against merely corrupt -- has to survive the file being
    unreadable for either reason.
    """
    tail = data[-8192:]
    if _PDF_ENCRYPT_RE.search(data) or b"/Encrypt" in tail:
        return _problem_verdict(
            FileKind.PDF,
            Code.FILE_ENCRYPTED,
            "This is a password-protected PDF, so its content cannot be read.",
            _REMEDIES_ENCRYPTED,
            size=size,
            format_name="pdf",
        )
    if b"%%EOF" not in tail and b"startxref" not in tail:
        return _problem_verdict(
            FileKind.PDF,
            Code.FILE_CORRUPT,
            "This PDF is missing its end marker, so it was truncated or damaged.",
            _REMEDIES_CORRUPT,
            size=size,
            format_name="pdf",
        )
    return _unsupported(
        FileKind.PDF,
        "pdf",
        size,
        _REMEDIES_DOCUMENT,
        "This is a PDF. EchoAct reads plain text only and does not extract text from a page "
        "layout.",
    )


_ZIP_CONTENT_RULES: Final = (
    ("word/document.xml", FileKind.DOCX, "docx", "Word document"),
    ("xl/workbook.xml", FileKind.XLSX, "xlsx", "Excel workbook"),
    ("ppt/presentation.xml", FileKind.PPTX, "pptx", "PowerPoint presentation"),
    ("Contents/content.hpf", FileKind.HWPX, "hwpx", "HWPX document"),
    ("Contents/section0.xml", FileKind.HWPX, "hwpx", "HWPX document"),
    ("META-INF/container.xml", FileKind.EPUB, "epub", "EPUB book"),
)

_MIMETYPE_RULES: Final = (
    (b"application/epub+zip", FileKind.EPUB, "epub", "EPUB book"),
    (b"application/hwp+zip", FileKind.HWPX, "hwpx", "HWPX document"),
    (
        b"application/vnd.oasis.opendocument",
        FileKind.OPENDOCUMENT,
        "opendocument",
        "OpenDocument file",
    ),
)


def _zip_verdict(data: bytes, size: int) -> Verdict:
    """A ZIP container, and which document format it holds.

    Section 2.7 lists DOCX, HWPX and EPUB as separate formats and all three
    are ZIP files, so the signature alone cannot tell them apart and the
    entry names have to be read.  Only the central directory and, at most,
    the tiny ``mimetype`` entry are touched: nothing else is extracted,
    because Section 2.7 puts decompression out of scope and a file that
    cannot be read must stay unread.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            names = {info.filename for info in infos}
            encrypted = any(info.flag_bits & 0x1 for info in infos)
            mimetype = b""
            if "mimetype" in names:
                try:
                    mimetype = zf.read("mimetype")[:128]
                except (RuntimeError, zipfile.BadZipFile, OSError, ValueError):
                    mimetype = b""
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, IndexError):
        return _problem_verdict(
            FileKind.ARCHIVE,
            Code.FILE_CORRUPT,
            "This file begins like a ZIP container, but its index could not be read, so it is "
            "damaged or incomplete.",
            _REMEDIES_CORRUPT,
            size=size,
            format_name="zip",
        )

    kind, format_name, label = FileKind.ARCHIVE, "zip", "ZIP archive"
    for prefix, mime_kind, mime_format, mime_label in _MIMETYPE_RULES:
        if mimetype.startswith(prefix):
            kind, format_name, label = mime_kind, mime_format, mime_label
            break
    else:
        for entry, rule_kind, rule_format, rule_label in _ZIP_CONTENT_RULES:
            if entry in names:
                kind, format_name, label = rule_kind, rule_format, rule_label
                break
        else:
            if "META-INF/MANIFEST.MF" in names:
                kind, format_name, label = FileKind.EXECUTABLE, "jar", "Java archive"

    if encrypted:
        return _problem_verdict(
            kind,
            Code.FILE_ENCRYPTED,
            f"This {label} has password-protected contents, so they cannot be read.",
            _REMEDIES_ENCRYPTED,
            size=size,
            format_name=format_name,
        )
    if kind is FileKind.ARCHIVE:
        return _unsupported(
            kind,
            format_name,
            size,
            _REMEDIES_ARCHIVE,
            "This is a ZIP archive. EchoAct does not unpack archives.",
        )
    if kind is FileKind.EXECUTABLE:
        return _unsupported(
            kind,
            format_name,
            size,
            _REMEDIES_EXECUTABLE,
            "This is a Java archive, which is a program rather than a document.",
        )
    return _unsupported(
        kind,
        format_name,
        size,
        _REMEDIES_DOCUMENT,
        f"This is a {label}. Its text is stored inside a document package that this version "
        "does not open.",
    )


_OLE_STREAM_RULES: Final = (
    ("WordDocument", FileKind.DOC, "doc", "Word 97-2003 document"),
    ("Workbook", FileKind.XLS, "xls", "Excel 97-2003 workbook"),
    ("PowerPoint Document", FileKind.PPT, "ppt", "PowerPoint 97-2003 presentation"),
)


def _ole_verdict(data: bytes, size: int) -> Verdict:
    """A legacy compound-file document: DOC, XLS, PPT, HWP, or an encrypted
    OOXML file, which Office stores in this container rather than as a ZIP.

    HWP declares itself in its ``FileHeader`` stream, whose 32-byte signature
    is followed by a version word and a property word; bit 1 of that property
    word is the password flag, which is how an encrypted HWP is told from an
    ordinary one without parsing the container's allocation tables.
    """
    if _u16("EncryptedPackage") in data:
        return _problem_verdict(
            FileKind.OLE_DOCUMENT,
            Code.FILE_ENCRYPTED,
            "This Office document is password-protected, so its text cannot be read.",
            _REMEDIES_ENCRYPTED,
            size=size,
            format_name="ooxml-encrypted",
        )

    hwp_at = data.find(b"HWP Document File")
    if hwp_at >= 0:
        properties_at = hwp_at + 36
        properties = (
            int.from_bytes(data[properties_at : properties_at + 4], "little")
            if properties_at + 4 <= len(data)
            else 0
        )
        if properties & 0x02:
            return _problem_verdict(
                FileKind.HWP,
                Code.FILE_ENCRYPTED,
                "This HWP document is password-protected, so its text cannot be read.",
                _REMEDIES_ENCRYPTED,
                size=size,
                format_name="hwp",
            )
        return _unsupported(
            FileKind.HWP,
            "hwp",
            size,
            _REMEDIES_DOCUMENT,
            "This is an HWP document. Its text is stored in a binary document format that this "
            "version does not open.",
        )

    for stream, kind, format_name, label in _OLE_STREAM_RULES:
        if _u16(stream) in data:
            return _unsupported(
                kind,
                format_name,
                size,
                _REMEDIES_DOCUMENT,
                f"This is a {label}. Its text is stored in a binary document format that this "
                "version does not open.",
            )
    return _unsupported(
        FileKind.OLE_DOCUMENT,
        "ole",
        size,
        _REMEDIES_DOCUMENT,
        "This is a legacy compound-file document. Its text is stored in a binary format that "
        "this version does not open.",
    )


def _markup_verdict(text: str, size: int) -> Verdict | None:
    """HTML and XML decode perfectly and are still refused.

    Section 2.7 puts web pages out of scope, and markup read aloud is tag
    names rather than prose.  Only a document that *begins* as markup is
    caught, so Markdown with an inline tag part-way through stays readable.
    """
    head = text[:1024].lstrip("\ufeff \t\r\n")
    lowered = head.lower()
    if lowered.startswith("{\\rtf"):
        return _unsupported(
            FileKind.RTF,
            "rtf",
            size,
            _REMEDIES_DOCUMENT,
            "This is a Rich Text Format document, which stores its text among formatting "
            "commands that this version does not interpret.",
        )
    if lowered.startswith(_HTML_STARTS):
        return _unsupported(
            FileKind.HTML,
            "html",
            size,
            _REMEDIES_DOCUMENT,
            "This is an HTML page. EchoAct does not fetch or interpret web pages.",
        )
    if lowered.startswith("<?xml"):
        is_html = "<html" in lowered
        return _unsupported(
            FileKind.HTML if is_html else FileKind.XML,
            "xhtml" if is_html else "xml",
            size,
            _REMEDIES_DOCUMENT,
            "This is a markup document. Reading it aloud would speak its tags rather than its "
            "text.",
        )
    return None


# -------------------------------------------------------------- encoding ---

_BOMS: Final = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def bom_encoding(data: bytes) -> str | None:
    """The codec a file's byte-order mark calls for, if it has one.

    Two details are deliberate.  UTF-32's little-endian mark begins with
    UTF-16's, so the four-byte marks are tested first; the other order turns
    every UTF-32 file into UTF-16 text full of NUL characters.  And the codec
    named is the endianness-detecting one rather than the explicit ``-le`` or
    ``-be`` form, because only the former consumes the mark: the explicit
    codecs leave it in the text as a zero-width character at offset 0, which
    would silently shift every 4.2 offset by one.
    """
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            return encoding
    return None


def _try_decode(data: bytes, encoding: str) -> tuple[str | None, int | None, str | None]:
    try:
        return data.decode(encoding), None, None
    except UnicodeDecodeError as exc:
        return None, exc.start, exc.reason
    except LookupError:
        return None, None, "unknown encoding"


def _candidate(data: bytes, encoding: str) -> EncodingCandidate:
    text, offset, reason = _try_decode(data, encoding)
    label = ENCODING_LABELS.get(encoding, encoding.upper())
    if text is not None:
        return EncodingCandidate(
            name=encoding, label=label, decodes_cleanly=True, preview=_preview_of(text)
        )
    lossy = data[: PREVIEW_CODEPOINTS * 4].decode(encoding, errors="replace")
    return EncodingCandidate(
        name=encoding,
        label=label,
        decodes_cleanly=False,
        preview=_preview_of(lossy),
        failure_offset=offset,
        failure_reason=reason,
    )


def verdict_for_text(
    text: str,
    encoding: str,
    *,
    byte_size: int,
    filename: str | None = None,
    confidence: Confidence = Confidence.CERTAIN,
) -> Verdict:
    """Judge text that has already been decoded.

    Public because F-34's confirmed encoding arrives after this module has
    already given up: the loader decodes with the encoding the user chose and
    the result still has to face the checks every readable file faces, or
    "choose CP949" would become a way past F-35's refusal to interpret a
    binary file as text.
    """
    if _text_is_control_dense(text):
        return _problem_verdict(
            FileKind.BINARY,
            Code.FILE_NOT_TEXT,
            "This file decodes into control characters rather than readable text, so it is not "
            "a text file.",
            _REMEDIES_BINARY,
            size=byte_size,
            format_name=encoding,
        )
    markup = _markup_verdict(text, byte_size)
    if markup is not None:
        return markup

    extension = _extension(filename)
    detail: dict[str, Any] = {}
    if extension and extension in _NON_TEXT_EXTENSIONS:
        # F-32's disguise case in reverse: the content is text but the name
        # claims otherwise.  The content decides; the disagreement is only
        # recorded so F-35's confirmation can say why it is being asked.
        detail["extension_mismatch"] = True
    return Verdict(
        kind=FileKind.TEXT,
        readable_as_text=True,
        encoding=encoding,
        confidence=confidence,
        needs_confirmation=extension not in TEXT_EXTENSIONS,
        preview=_preview_of(text),
        byte_size=byte_size,
        format_name="text",
        detail=detail,
    )


def _sniff_text(data: bytes, filename: str | None, size: int) -> Verdict:
    """F-34's order: UTF-8, then CP949, then ask.

    Trying UTF-8 first is not a preference.  A CP949 file that happens to be
    valid UTF-8 is a curiosity; a UTF-8 file that happens to be valid CP949
    is routine, because CP949 accepts nearly every high-byte pair.  Reversing
    the order would read ordinary Korean UTF-8 as hanja soup.
    """
    utf8_text, utf8_offset, utf8_reason = _try_decode(data, "utf-8")
    if utf8_text is not None:
        # Pure ASCII is the one case with nothing to be wrong about: CP949
        # agrees with UTF-8 byte for byte below 0x80.
        confidence = Confidence.CERTAIN if data.isascii() else Confidence.LIKELY
        return verdict_for_text(
            utf8_text, "utf-8", byte_size=size, filename=filename, confidence=confidence
        )

    cp949_text, _offset, _reason = _try_decode(data, "cp949")
    if cp949_text is not None:
        return verdict_for_text(
            cp949_text, "cp949", byte_size=size, filename=filename, confidence=Confidence.LIKELY
        )

    candidates = tuple(_candidate(data, name) for name in AUTO_ENCODINGS)
    return _problem_verdict(
        FileKind.TEXT,
        Code.FILE_ENCODING,
        "This file's text encoding could not be determined: it is neither valid UTF-8 nor valid "
        "CP949, so it is in some other encoding or partly damaged.",
        _REMEDIES_ENCODING,
        size=size,
        format_name="text",
        candidates=candidates,
        # F-35 still applies once an encoding is chosen, so the flag is
        # computed here too rather than being lost with the failed decode.
        needs_confirmation=_extension(filename) not in TEXT_EXTENSIONS,
        preview=candidates[0].preview,
        preview_lossy=True,
        detail={
            "utf8_failure_offset": utf8_offset,
            "utf8_failure_reason": utf8_reason,
            "selectable_encodings": list(SELECTABLE_ENCODINGS),
        },
    )


# ----------------------------------------------------------- entry point ---


def sniff(data: bytes, *, filename: str | None = None) -> Verdict:
    """Decide what ``data`` is and whether it may become text (F-32, F-34, F-35).

    ``filename`` is advisory only.  It never makes a file readable and never
    makes one unreadable; it decides nothing but whether F-35 asks the user to
    confirm first, which is the only role F-32 leaves to an extension.
    """
    size = len(data)
    if size == 0:
        return _problem_verdict(
            FileKind.EMPTY,
            Code.INPUT_EMPTY,
            "This file is empty.",
            _REMEDIES_EMPTY,
            size=size,
        )

    declared = bom_encoding(data)
    if declared is not None:
        # A byte-order mark is the file speaking for itself, so this is the
        # one place an encoding is accepted without trial -- and the one case
        # where NUL bytes do not mean "binary", UTF-16 text being full of
        # them.  F-37 still holds: nothing was chosen on the caller's behalf.
        text, offset, reason = _try_decode(data, declared)
        if text is None:
            return _problem_verdict(
                FileKind.TEXT,
                Code.FILE_CORRUPT,
                f"This file declares {ENCODING_LABELS.get(declared, declared)} but does not "
                "decode as it, so it is damaged or was cut short.",
                _REMEDIES_CORRUPT,
                size=size,
                format_name=declared,
                detail={"failure_offset": offset, "failure_reason": reason},
            )
        return verdict_for_text(
            text, declared, byte_size=size, filename=filename, confidence=Confidence.CERTAIN
        )

    structural = _detect_binary(data, size)
    if structural is not None:
        return structural
    return _sniff_text(data, filename, size)


def _detect_binary(data: bytes, size: int) -> Verdict | None:
    """Everything decided by the bytes' shape rather than by decoding them.

    Returns ``None`` when the file is still a candidate for being text.
    """
    if data.startswith(b"%PDF-"):
        return _pdf_verdict(data, size)
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _zip_verdict(data, size)
    if data.startswith(_OLE_MAGIC):
        return _ole_verdict(data, size)
    if data.startswith(b"SQLite format 3\x00"):
        return _unsupported(
            FileKind.DATABASE,
            "sqlite",
            size,
            _REMEDIES_BINARY,
            "This is a database file, not a document.",
        )

    image = _image_format(data)
    if image:
        return _unsupported(
            FileKind.IMAGE,
            image,
            size,
            _REMEDIES_IMAGE,
            f"This is a {image.upper()} image. EchoAct cannot read text that is part of a "
            "picture or a scan.",
        )

    executable = _executable_format(data)
    if executable:
        return _unsupported(
            FileKind.EXECUTABLE,
            executable,
            size,
            _REMEDIES_EXECUTABLE,
            "This is a program file, not a document.",
        )

    media_format, media_kind = _media_format(data)
    if media_format:
        noun = "an audio file" if media_kind is FileKind.AUDIO else "a video file"
        return _unsupported(
            media_kind,
            media_format,
            size,
            _REMEDIES_MEDIA,
            f"This is {noun} ({media_format}). EchoAct generates speech but does not listen to "
            "it.",
        )

    archive = _archive_format(data)
    if archive:
        return _unsupported(
            FileKind.ARCHIVE,
            archive,
            size,
            _REMEDIES_ARCHIVE,
            f"This is a {archive} archive. EchoAct does not unpack archives.",
        )

    window = data[:SNIFF_WINDOW_BYTES]
    if b"\x00" in window:
        return _problem_verdict(
            FileKind.BINARY,
            Code.FILE_NOT_TEXT,
            "This file contains binary data rather than text.",
            _REMEDIES_BINARY,
            size=size,
            format_name="binary",
            detail={"first_nul_offset": window.index(b"\x00")},
        )
    if _byte_window_is_control_dense(window):
        return _problem_verdict(
            FileKind.BINARY,
            Code.FILE_NOT_TEXT,
            "This file is mostly control characters, so it is not a text file.",
            _REMEDIES_BINARY,
            size=size,
            format_name="binary",
        )
    return None


__all__ = [
    "AUTO_ENCODINGS",
    "ENCODING_LABELS",
    "PREVIEW_CODEPOINTS",
    "SELECTABLE_ENCODINGS",
    "SNIFF_WINDOW_BYTES",
    "SUPPORTED_FORMATS",
    "TEXT_EXTENSIONS",
    "Confidence",
    "EncodingCandidate",
    "FileKind",
    "Verdict",
    "bom_encoding",
    "sniff",
    "verdict_for_text",
]
