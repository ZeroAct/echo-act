"""What ``echoact.text.loader`` must do with a file it is asked to open.

Covers F-02 (TXT and Markdown, UTF-8 and CP949), F-03 (the 2,000,000-byte
and 50,000-code-point limits), F-32 (nothing changes when an open fails),
F-34 (an encoding is applied only once it is confirmed), F-35 (confirmation
before a non-standard file becomes text), F-36 (no truncation) and F-37 (the
upload path gets the identical treatment and is never given an encoding).
Acceptance item A-01 is the scenario these are written against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echoact import paths
from echoact.errors import Code, EchoActError
from echoact.policy import MAX_IMPORT_FILE_BYTES, MAX_INPUT_CODEPOINTS
from echoact.text import loader as loader_module
from echoact.text.loader import (
    inspect_bytes,
    inspect_file,
    load_bytes,
    load_file,
    normalise_newlines,
)
from echoact.text.sniff import FileKind

KOREAN = "안녕하세요. 오늘도 좋은 하루 보내세요."


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """No test may read or write the real per-user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


def write(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------- reading ---


def test_a_utf8_text_file_is_read_as_it_was_written(tmp_path):
    path = write(tmp_path, "story.txt", KOREAN.encode("utf-8"))
    loaded = load_file(path)
    assert loaded.text == KOREAN
    assert loaded.encoding == "utf-8"
    assert loaded.kind is FileKind.TEXT
    assert loaded.codepoints == len(KOREAN)
    assert not loaded.line_endings_normalised


def test_a_cp949_text_file_is_read_without_being_asked_about(tmp_path):
    path = write(tmp_path, "story.txt", KOREAN.encode("cp949"))
    loaded = load_file(path)
    assert loaded.text == KOREAN
    assert loaded.encoding == "cp949"


def test_a_markdown_file_is_read_as_plain_text_and_not_rendered(tmp_path):
    body = "# Heading\n\n**bold** text\n"
    path = write(tmp_path, "notes.md", body.encode("utf-8"))
    loaded = load_file(path)
    assert loaded.text == body


def test_a_byte_order_mark_is_consumed_rather_than_read_aloud(tmp_path):
    path = write(tmp_path, "story.txt", "Hello".encode("utf-8-sig"))
    loaded = load_file(path)
    assert loaded.text == "Hello"
    assert loaded.had_bom


def test_windows_line_endings_are_folded_and_the_change_is_recorded(tmp_path):
    path = write(tmp_path, "story.txt", b"one\r\ntwo\r\nthree")
    loaded = load_file(path)
    assert loaded.text == "one\ntwo\nthree"
    assert loaded.line_endings_normalised
    assert loaded.crlf_count == 2
    assert loaded.cr_count == 0
    # The count F-03 shows is of the text the reader will see, not of the
    # bytes: two carriage returns are gone from it.
    assert loaded.codepoints == 13
    assert loaded.byte_size == 15


def test_old_style_carriage_returns_are_folded_too(tmp_path):
    path = write(tmp_path, "story.txt", b"one\rtwo\r")
    loaded = load_file(path)
    assert loaded.text == "one\ntwo\n"
    assert loaded.cr_count == 2


def test_normalise_newlines_reports_what_it_changed():
    text, crlf, cr = normalise_newlines("a\r\nb\rc\nd")
    assert text == "a\nb\nc\nd"
    assert (crlf, cr) == (1, 1)


# ---------------------------------------------------------------- limits ---


def test_a_file_over_two_million_bytes_is_refused_without_being_read(tmp_path, monkeypatch):
    path = write(tmp_path, "big.txt", b"a" * (MAX_IMPORT_FILE_BYTES + 1))

    def refuse_to_open(self, *args, **kwargs):
        raise AssertionError("F-03 requires the size check before the file is opened")

    monkeypatch.setattr(Path, "open", refuse_to_open)
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.FILE_TOO_LARGE
    assert caught.value.detail["limit_bytes"] == MAX_IMPORT_FILE_BYTES


def test_a_file_exactly_at_the_byte_limit_passes_the_size_gate(tmp_path):
    path = write(tmp_path, "big.txt", b"a" * MAX_IMPORT_FILE_BYTES)
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    # It is refused, but for its length in characters rather than in bytes:
    # the two limits in F-03 are separate and both are reported honestly.
    assert caught.value.code is Code.INPUT_TOO_LONG


def test_fifty_thousand_code_points_are_accepted(tmp_path):
    body = "가" * MAX_INPUT_CODEPOINTS
    path = write(tmp_path, "long.txt", body.encode("utf-8"))
    loaded = load_file(path)
    assert loaded.codepoints == MAX_INPUT_CODEPOINTS


def test_one_code_point_too_many_is_refused_and_nothing_is_truncated(tmp_path):
    body = "가" * (MAX_INPUT_CODEPOINTS + 1)
    path = write(tmp_path, "long.txt", body.encode("utf-8"))
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.INPUT_TOO_LONG
    assert caught.value.detail["codepoints"] == MAX_INPUT_CODEPOINTS + 1
    assert caught.value.detail["limit_codepoints"] == MAX_INPUT_CODEPOINTS
    assert not caught.value.retryable


def test_the_limit_counts_code_points_and_not_utf8_bytes(tmp_path):
    body = "가" * 30_000  # 90,000 bytes, well under 50,000 characters
    path = write(tmp_path, "long.txt", body.encode("utf-8"))
    assert load_file(path).codepoints == 30_000


def test_a_file_of_only_whitespace_is_empty_input(tmp_path):
    path = write(tmp_path, "blank.txt", b"   \n\t\r\n  ")
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.INPUT_EMPTY


def test_an_empty_file_is_empty_input(tmp_path):
    path = write(tmp_path, "blank.txt", b"")
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.INPUT_EMPTY


# -------------------------------------------------------------- encoding ---


def test_an_undecidable_encoding_is_a_question_with_candidates(tmp_path):
    path = write(tmp_path, "story.txt", "café naïve".encode("latin-1"))
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    error = caught.value
    assert error.code is Code.FILE_ENCODING
    names = [c["encoding"] for c in error.detail["encoding_candidates"]]
    assert names == ["utf-8", "cp949"]
    assert error.detail["remedies"]


def test_the_chosen_encoding_is_applied_only_when_it_is_given(tmp_path):
    # UTF-16 with no byte-order mark is the case F-34 exists for: nothing in
    # the bytes says what it is, and half of them are NUL.
    body = "가나다 hello"
    path = write(tmp_path, "story.txt", body.encode("utf-16-le"))
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    # An encoding problem rather than "not text": the distinction is the
    # whole of F-34, because it decides whether the user is offered the
    # encoding selector or told the file cannot be read at all.
    assert caught.value.code is Code.FILE_ENCODING

    loaded = load_file(path, encoding="utf-16-le")
    assert loaded.text == body
    assert loaded.encoding == "utf-16-le"


def test_a_chosen_encoding_is_still_judged_on_what_it_produces(tmp_path):
    path = write(tmp_path, "blob.txt", bytes(range(0, 32)) * 4)
    with pytest.raises(EchoActError) as caught:
        load_file(path, encoding="utf-8", confirm_unknown=True)
    assert caught.value.code is Code.FILE_NOT_TEXT


def test_a_chosen_encoding_that_does_not_fit_is_refused_not_repaired(tmp_path):
    path = write(tmp_path, "story.txt", "café naïve".encode("latin-1"))
    with pytest.raises(EchoActError) as caught:
        load_file(path, encoding="utf-8")
    assert caught.value.code is Code.FILE_ENCODING
    assert caught.value.detail["failure_offset"] == 3


def test_an_encoding_echoact_does_not_offer_is_refused(tmp_path):
    path = write(tmp_path, "story.txt", "café".encode("latin-1"))
    with pytest.raises(EchoActError) as caught:
        load_file(path, encoding="latin-1")
    assert caught.value.code is Code.FILE_ENCODING
    assert "utf-8" in caught.value.detail["selectable_encodings"]


def test_no_text_is_ever_returned_with_replacement_characters(tmp_path):
    path = write(tmp_path, "story.txt", b"good text \xed\xa0\x80 bad")
    with pytest.raises(EchoActError):
        load_file(path)
    for candidate in ("utf-8", "cp949", "utf-16"):
        with pytest.raises(EchoActError):
            load_file(path, encoding=candidate)


def test_choosing_an_encoding_cannot_turn_a_picture_into_text(tmp_path):
    path = write(tmp_path, "scan.png", b"\x89PNG\r\n\x1a\n" + b"\x01" * 64)
    for encoding in (None, "cp949"):
        with pytest.raises(EchoActError) as caught:
            load_file(path, encoding=encoding, confirm_unknown=True)
        assert caught.value.code is Code.FILE_UNSUPPORTED
        assert caught.value.detail["kind"] == FileKind.IMAGE.value


# ------------------------------------------------------- F-35 confirmation ---


def test_a_file_with_no_extension_is_not_opened_until_it_is_confirmed(tmp_path):
    path = write(tmp_path, "README", b"plain words\n")
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.detail["needs_confirmation"] is True
    assert caught.value.detail["preview"] == "plain words\n"

    loaded = load_file(path, confirm_unknown=True)
    assert loaded.text == "plain words\n"
    assert loaded.confirmed_as_text


def test_confirming_does_not_make_a_binary_file_readable(tmp_path):
    path = write(tmp_path, "blob", b"binary \x00 data")
    with pytest.raises(EchoActError) as caught:
        load_file(path, confirm_unknown=True)
    assert caught.value.code is Code.FILE_NOT_TEXT


def test_a_text_file_wearing_a_pdf_name_is_confirmed_before_it_is_opened(tmp_path):
    path = write(tmp_path, "notes.pdf", b"actually just prose\n")
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.detail["extension_mismatch"] is True
    assert load_file(path, confirm_unknown=True).text == "actually just prose\n"


def test_inspect_file_offers_the_preview_the_confirmation_needs(tmp_path):
    path = write(tmp_path, "notes", KOREAN.encode("cp949"))
    verdict = inspect_file(path)
    assert verdict.readable_as_text
    assert verdict.needs_confirmation
    assert verdict.encoding == "cp949"
    assert verdict.preview.startswith("안녕하세요")


def test_inspect_file_reports_rather_than_raises_for_an_unreadable_file(tmp_path):
    verdict = inspect_file(tmp_path / "absent.txt")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_NOT_FOUND
    assert verdict.problem.remedies


# ------------------------------------------------------- filesystem cases ---


def test_a_missing_file_is_reported_as_missing(tmp_path):
    with pytest.raises(EchoActError) as caught:
        load_file(tmp_path / "absent.txt")
    assert caught.value.code is Code.FILE_NOT_FOUND


def test_a_folder_is_not_a_document(tmp_path):
    folder = tmp_path / "somewhere"
    folder.mkdir()
    with pytest.raises(EchoActError) as caught:
        load_file(folder)
    assert caught.value.code is Code.FILE_UNSUPPORTED


def test_a_file_the_user_may_not_read_is_a_permission_problem(tmp_path, monkeypatch):
    path = write(tmp_path, "story.txt", b"secret")
    real_open = Path.open

    def deny(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.FILE_PERMISSION
    assert caught.value.detail["remedies"]


def test_a_file_that_grew_past_the_limit_after_the_size_check_is_still_refused(
    tmp_path, monkeypatch
):
    path = write(tmp_path, "story.txt", b"small")
    oversized = b"a" * (MAX_IMPORT_FILE_BYTES + 1)
    real_open = Path.open

    class Grown:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return oversized[:size] if size and size > 0 else oversized

    def grow(self, *args, **kwargs):
        if self == path:
            return Grown()
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow)
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    assert caught.value.code is Code.FILE_TOO_LARGE


# ------------------------------------------------------------ F-32 purity ---


_FAILING_SAMPLES: dict[str, tuple[str, bytes]] = {
    "image": ("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x01" * 32),
    "encoding": ("story.txt", "café".encode("latin-1")),
    "too-long": ("long.txt", "가".encode() * (MAX_INPUT_CODEPOINTS + 1)),
    "blank": ("blank.txt", b"  \n "),
    "unconfirmed": ("mystery", b"prose with no extension\n"),
}


@pytest.mark.parametrize("sample", sorted(_FAILING_SAMPLES))
def test_a_failed_open_leaves_the_file_and_the_data_directory_untouched(tmp_path, sample):
    name, payload = _FAILING_SAMPLES[sample]
    path = write(tmp_path, name, payload)
    before = sorted(p.name for p in tmp_path.iterdir())
    stamp = path.stat().st_mtime_ns

    with pytest.raises(EchoActError):
        load_file(path)

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == stamp
    assert not paths.data_dir().exists()


def test_reading_a_file_creates_nothing_of_its_own(tmp_path):
    path = write(tmp_path, "story.txt", KOREAN.encode("utf-8"))
    load_file(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["story.txt"]
    assert not paths.data_dir().exists()


@pytest.mark.parametrize(
    "forbidden",
    ["import socket", "import urllib", "import http", "import requests", "import httpx",
     "urlopen", "http://", "https://", "subprocess", "errors=\"replace\""],
)
def test_the_module_neither_reaches_the_network_nor_repairs_text(forbidden):
    source = Path(loader_module.__file__).read_text(encoding="utf-8")
    assert forbidden not in source


@pytest.mark.parametrize("forbidden", ["rename", "online", "upload to", "internet", "cloud"])
def test_no_remedy_suggests_a_rename_or_an_external_service(forbidden):
    remedies: list[str] = []
    for name in dir(loader_module):
        if name.startswith("_REMEDIES"):
            remedies.extend(getattr(loader_module, name))
    assert remedies
    assert not [r for r in remedies if forbidden in r.lower()]


def test_an_error_payload_carries_no_path_and_no_file_name(tmp_path):
    path = write(tmp_path, "secret-plans.pdf", b"%PDF-1.7\nbody\nstartxref\n9\n%%EOF\n")
    with pytest.raises(EchoActError) as caught:
        load_file(path)
    payload = repr(caught.value.to_payload("req_1"))
    assert "secret-plans" not in payload
    assert str(tmp_path) not in payload


# ------------------------------------------------------------ F-37 upload ---


def test_an_upload_takes_the_same_path_as_a_chosen_file(tmp_path):
    payload = KOREAN.encode("cp949")
    path = write(tmp_path, "story.txt", payload)
    assert load_bytes(payload, filename="story.txt").text == load_file(path).text


def test_an_upload_over_the_limit_is_refused_with_the_same_code():
    with pytest.raises(EchoActError) as caught:
        load_bytes(b"a" * (MAX_IMPORT_FILE_BYTES + 1), filename="big.txt")
    assert caught.value.code is Code.FILE_TOO_LARGE


def test_a_non_interactive_caller_is_never_given_an_encoding_it_did_not_ask_for():
    with pytest.raises(EchoActError) as caught:
        load_bytes("café naïve".encode("latin-1"), filename="story.txt")
    error = caught.value
    assert error.code is Code.FILE_ENCODING
    assert [c["encoding"] for c in error.detail["encoding_candidates"]] == ["utf-8", "cp949"]
    assert error.detail["selectable_encodings"]


def test_the_encoding_error_a_caller_receives_is_correctable():
    body = "가나다 hello"
    with pytest.raises(EchoActError):
        load_bytes(body.encode("utf-16-le"), filename="story.txt")
    corrected = load_bytes(body.encode("utf-16-le"), filename="story.txt", encoding="utf-16-le")
    assert corrected.text == body


def test_an_upload_with_no_file_name_still_asks_before_being_treated_as_text():
    with pytest.raises(EchoActError) as caught:
        load_bytes(b"some prose\n")
    assert caught.value.detail["needs_confirmation"] is True
    assert load_bytes(b"some prose\n", confirm_unknown=True).text == "some prose\n"


def test_inspect_bytes_applies_the_byte_limit_before_looking_at_content():
    verdict = inspect_bytes(b"a" * (MAX_IMPORT_FILE_BYTES + 1), filename="big.txt")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_TOO_LARGE


def test_an_uploaded_docx_is_refused_with_guidance_rather_than_extracted():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("word/document.xml", b"<w:document/>")

    with pytest.raises(EchoActError) as caught:
        load_bytes(buffer.getvalue(), filename="report.docx")
    error = caught.value
    assert error.code is Code.FILE_UNSUPPORTED
    assert error.http_status == 415
    assert error.detail["supported_formats"] == ["TXT", "Markdown"]
    assert len(error.detail["remedies"]) >= 2
