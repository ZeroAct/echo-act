"""What ``echoact.text.sniff`` must decide about a file's bytes.

Covers F-32 (the extension is not trusted, and unsupported, corrupt,
encrypted and encoding cases are told apart), F-33 (what the refusal says,
and what it may never say), F-34 (UTF-8 then CP949, then a question), and
F-35 (binary, executable and archive files are never forced into text).
Acceptance item A-05 is the scenario these are written against.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from echoact import paths
from echoact.errors import Code
from echoact.text import sniff as sniff_module
from echoact.text.sniff import Confidence, FileKind, sniff

KOREAN = "안녕하세요. 오늘은 날씨가 좋습니다."
MIXED = "EchoAct는 Korean과 English를 읽습니다.\n"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """No test may read or write the real per-user data directory."""
    monkeypatch.setenv("ECHOACT_DATA_DIR", str(tmp_path / "data"))
    paths.data_dir.cache_clear()
    yield
    paths.data_dir.cache_clear()


# ------------------------------------------------------------- builders ---


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


def mark_zip_entries_encrypted(raw: bytes) -> bytes:
    """Set the general-purpose "encrypted" bit that a real password sets.

    ``zipfile`` cannot write an encrypted archive, and the flag is what the
    sniffer reads, so the flag is what the test supplies.
    """
    data = bytearray(raw)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while True:
            at = data.find(signature, start)
            if at < 0:
                break
            data[at + flag_offset] = data[at + flag_offset] | 0x01
            start = at + 4
    return bytes(data)


def ole_bytes(payload: bytes) -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 24 + payload + b"\x00" * 64


def hwp_bytes(*, password: bool) -> bytes:
    header = b"HWP Document File".ljust(32, b"\x00")
    version = (0x05000000).to_bytes(4, "little")
    properties = (0x02 if password else 0x00).to_bytes(4, "little")
    return ole_bytes(header + version + properties)


def pe_bytes() -> bytes:
    data = bytearray(b"MZ" + b"\x00" * 0x7E)
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\x00\x00"
    return bytes(data)


def pdf_bytes(*, body: bytes = b"1 0 obj\n<< /Type /Catalog >>\nendobj\n", trailer: bytes = b"trailer\n<< /Size 4 >>\nstartxref\n9\n%%EOF\n") -> bytes:
    return b"%PDF-1.7\n" + body + trailer


# ------------------------------------------------------------- encodings ---


def test_plain_ascii_is_certain_and_needs_no_encoding_question():
    verdict = sniff(b"Hello there.\n", filename="note.txt")
    assert verdict.ok
    assert verdict.kind is FileKind.TEXT
    assert verdict.encoding == "utf-8"
    assert verdict.confidence is Confidence.CERTAIN
    assert verdict.candidates == ()


def test_utf8_korean_is_read_as_utf8_and_not_as_cp949():
    verdict = sniff(KOREAN.encode("utf-8"), filename="story.txt")
    assert verdict.ok
    assert verdict.encoding == "utf-8"
    assert verdict.preview.startswith("안녕하세요")


def test_cp949_korean_is_read_once_utf8_has_failed():
    verdict = sniff(KOREAN.encode("cp949"), filename="story.txt")
    assert verdict.ok
    assert verdict.encoding == "cp949"
    assert verdict.preview.startswith("안녕하세요")


def test_a_byte_order_mark_settles_the_encoding_without_a_trial():
    verdict = sniff(MIXED.encode("utf-8-sig"), filename="story.txt")
    assert verdict.ok
    assert verdict.encoding == "utf-8-sig"
    assert verdict.confidence is Confidence.CERTAIN
    assert not verdict.preview.startswith("\ufeff")


def test_utf16_text_is_not_binary_despite_its_nul_bytes():
    verdict = sniff(MIXED.encode("utf-16"), filename="story.txt")
    assert verdict.ok
    assert verdict.encoding == "utf-16"
    assert verdict.preview.startswith("EchoAct")


def test_a_utf32_mark_is_not_mistaken_for_utf16():
    data = "abc".encode("utf-32")
    verdict = sniff(data, filename="story.txt")
    assert verdict.ok
    assert verdict.encoding == "utf-32"
    assert verdict.preview == "abc"


def test_a_declared_encoding_that_does_not_decode_is_damage_not_a_guess():
    data = b"\xff\xfe" + b"A\x00B\x00\xff\xdc"  # a lone low surrogate
    verdict = sniff(data, filename="story.txt")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_CORRUPT


def test_bytes_that_are_neither_utf8_nor_cp949_become_a_question():
    data = "café naïve".encode("latin-1")
    verdict = sniff(data, filename="story.txt")

    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_ENCODING
    assert [c.name for c in verdict.candidates] == ["utf-8", "cp949"]
    assert not any(c.decodes_cleanly for c in verdict.candidates)
    assert all(c.failure_offset is not None for c in verdict.candidates)
    # F-34: the preview may show the damage, but it is labelled as lossy and
    # no repaired text is offered as content.
    assert verdict.preview_lossy
    assert not verdict.readable_as_text


def test_the_encoding_question_offers_a_preview_for_each_candidate():
    verdict = sniff("café".encode("latin-1"), filename="story.txt")
    assert all(c.preview for c in verdict.candidates)
    assert any("\ufffd" in c.preview for c in verdict.candidates)


# ---------------------------------------------------------- disguised ---


def test_a_pdf_named_txt_is_still_refused_as_a_pdf():
    verdict = sniff(pdf_bytes(), filename="innocent.txt")
    assert verdict.kind is FileKind.PDF
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_a_text_file_named_pdf_is_still_text_but_is_flagged_as_a_mismatch():
    verdict = sniff(b"just words\n", filename="disguised.pdf")
    assert verdict.readable_as_text
    assert verdict.detail["extension_mismatch"] is True
    assert verdict.needs_confirmation


def test_an_encrypted_pdf_is_told_apart_from_a_readable_one():
    data = pdf_bytes(trailer=b"trailer\n<< /Encrypt 5 0 R /Size 4 >>\nstartxref\n9\n%%EOF\n")
    verdict = sniff(data, filename="secret.pdf")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_ENCRYPTED


def test_a_truncated_pdf_is_reported_as_damaged_rather_than_unsupported():
    verdict = sniff(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n", filename="cut.pdf")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_CORRUPT


# ------------------------------------------------- zip-based documents ---


def test_docx_is_recognised_from_its_entry_names_not_its_name():
    data = zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document/>",
        }
    )
    verdict = sniff(data, filename="report.bin")
    assert verdict.kind is FileKind.DOCX
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_epub_is_recognised_from_its_mimetype_entry():
    data = zip_bytes(
        {
            "mimetype": b"application/epub+zip",
            "META-INF/container.xml": b"<container/>",
            "OEBPS/content.opf": b"<package/>",
        }
    )
    verdict = sniff(data, filename="book.epub")
    assert verdict.kind is FileKind.EPUB


def test_hwpx_is_recognised_from_its_package_entries():
    data = zip_bytes(
        {
            "mimetype": b"application/hwp+zip",
            "Contents/content.hpf": b"<opf/>",
            "Contents/section0.xml": b"<sec/>",
        }
    )
    verdict = sniff(data, filename="report.hwpx")
    assert verdict.kind is FileKind.HWPX


def test_a_plain_zip_is_refused_without_being_unpacked():
    verdict = sniff(zip_bytes({"a.txt": b"hello"}), filename="bundle.zip")
    assert verdict.kind is FileKind.ARCHIVE
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_a_password_protected_entry_is_encrypted_rather_than_corrupt():
    data = mark_zip_entries_encrypted(zip_bytes({"word/document.xml": b"<w:document/>"}))
    verdict = sniff(data, filename="locked.docx")
    assert verdict.kind is FileKind.DOCX
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_ENCRYPTED


def test_a_zip_whose_index_cannot_be_read_is_corrupt_rather_than_encrypted():
    verdict = sniff(b"PK\x03\x04" + b"\x00" * 40 + b"garbage", filename="broken.docx")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_CORRUPT


# ------------------------------------------- compound-file documents ---


def test_a_legacy_word_document_is_recognised_from_its_stream_name():
    verdict = sniff(ole_bytes("WordDocument".encode("utf-16-le")), filename="old.doc")
    assert verdict.kind is FileKind.DOC
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_an_hwp_document_is_recognised_and_is_not_called_encrypted():
    verdict = sniff(hwp_bytes(password=False), filename="report.hwp")
    assert verdict.kind is FileKind.HWP
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_an_hwp_password_flag_is_reported_as_encryption():
    verdict = sniff(hwp_bytes(password=True), filename="report.hwp")
    assert verdict.kind is FileKind.HWP
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_ENCRYPTED


def test_an_encrypted_office_package_is_reported_as_encrypted():
    verdict = sniff(ole_bytes("EncryptedPackage".encode("utf-16-le")), filename="locked.docx")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_ENCRYPTED


# ------------------------------------------------------ other families ---


@pytest.mark.parametrize(
    ("data", "expected_format"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "jpeg"),
        (b"GIF89a" + b"\x00" * 16, "gif"),
        (b"RIFF\x20\x00\x00\x00WEBPVP8 ", "webp"),
        (
            b"BM"
            + (54).to_bytes(4, "little")
            + b"\x00\x00\x00\x00"
            + (54).to_bytes(4, "little")
            + b"\x00" * 40,
            "bmp",
        ),
    ],
)
def test_images_are_refused_and_no_text_recognition_is_promised(data, expected_format):
    verdict = sniff(data, filename="scan.txt")
    assert verdict.kind is FileKind.IMAGE
    assert verdict.format_name == expected_format
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED
    assert any("recognition" in remedy for remedy in verdict.problem.remedies)


@pytest.mark.parametrize(
    ("data", "expected_kind"),
    [
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", FileKind.AUDIO),
        (b"ID3\x03\x00\x00\x00\x00\x00\x00", FileKind.AUDIO),
        (b"OggS\x00\x02\x00\x00\x00\x00\x00\x00", FileKind.AUDIO),
        (b"fLaC\x00\x00\x00\x22", FileKind.AUDIO),
        (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", FileKind.VIDEO),
        (b"RIFF\x24\x00\x00\x00AVI LIST", FileKind.VIDEO),
    ],
)
def test_audio_and_video_are_refused_without_offering_transcription(data, expected_kind):
    verdict = sniff(data, filename="clip.txt")
    assert verdict.kind is expected_kind
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED
    assert any("transcribe" in remedy for remedy in verdict.problem.remedies)


@pytest.mark.parametrize(
    ("data", "expected_format"),
    [
        (b"Rar!\x1a\x07\x00" + b"\x00" * 8, "rar"),
        (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8, "7z"),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 8, "gzip"),
        (b"\xfd7zXZ\x00" + b"\x00" * 8, "xz"),
        (b"x" * 257 + b"ustar\x0000", "tar"),
    ],
)
def test_archives_are_refused_and_nothing_is_unpacked(data, expected_format):
    verdict = sniff(data, filename="bundle.txt")
    assert verdict.kind is FileKind.ARCHIVE
    assert verdict.format_name == expected_format
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


@pytest.mark.parametrize(
    ("data", "expected_format"),
    [
        (pe_bytes(), "windows pe"),
        (b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16, "elf"),
        (b"\xcf\xfa\xed\xfe\x07\x00\x00\x01" + b"\x00" * 16, "mach-o"),
    ],
)
def test_executables_are_refused_and_never_run(data, expected_format):
    verdict = sniff(data, filename="setup.txt")
    assert verdict.kind is FileKind.EXECUTABLE
    assert verdict.format_name == expected_format
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED
    assert any("never runs" in remedy for remedy in verdict.problem.remedies)


def test_a_sentence_beginning_bm_is_not_taken_for_a_bitmap():
    verdict = sniff(b"BMW cars are made in Bavaria.\n", filename="cars.txt")
    assert verdict.ok
    assert verdict.kind is FileKind.TEXT


def test_a_nul_byte_makes_a_file_binary_whatever_it_is_called():
    verdict = sniff(b"text then \x00 a nul byte", filename="notes.txt")
    assert verdict.kind is FileKind.BINARY
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_NOT_TEXT
    assert verdict.detail["first_nul_offset"] == 10


def test_control_character_soup_is_not_text():
    verdict = sniff(b"\x01\x02\x03\x04\x05\x06\x07ab", filename="notes.txt")
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_NOT_TEXT


def test_a_stray_escape_in_a_long_log_does_not_condemn_it():
    verdict = sniff(("line\x1b[0m\n" + "ordinary log line\n" * 200).encode(), filename="run.txt")
    assert verdict.ok


def test_html_is_refused_even_though_it_decodes_perfectly():
    verdict = sniff(b"<!DOCTYPE html>\n<html><body>Hi</body></html>\n", filename="page.txt")
    assert verdict.kind is FileKind.HTML
    assert verdict.problem is not None
    assert verdict.problem.code is Code.FILE_UNSUPPORTED


def test_xml_is_refused_so_that_tags_are_not_read_aloud():
    verdict = sniff(b"<?xml version='1.0'?><doc><p>hi</p></doc>", filename="doc.txt")
    assert verdict.kind is FileKind.XML
    assert verdict.problem is not None


def test_markdown_with_an_inline_tag_part_way_through_is_still_text():
    body = b"# Title\n\nSome prose with an <em>inline</em> tag.\n"
    verdict = sniff(body, filename="notes.md")
    assert verdict.ok
    assert not verdict.needs_confirmation


def test_rtf_is_refused_as_a_document_format():
    verdict = sniff(rb"{\rtf1\ansi hello}", filename="letter.txt")
    assert verdict.kind is FileKind.RTF
    assert verdict.problem is not None


# ---------------------------------------------------------- F-35, F-33 ---


@pytest.mark.parametrize("filename", ["notes.txt", "notes.md", "notes.markdown"])
def test_a_recognised_text_name_needs_no_confirmation(filename):
    assert not sniff(b"plain words\n", filename=filename).needs_confirmation


@pytest.mark.parametrize("filename", [None, "notes", "notes.dat", "data.csv"])
def test_an_unknown_or_absent_extension_asks_for_confirmation_first(filename):
    verdict = sniff(b"plain words\n", filename=filename)
    assert verdict.readable_as_text
    assert verdict.needs_confirmation
    assert verdict.preview == "plain words\n"


def test_an_empty_file_is_reported_as_empty_input():
    verdict = sniff(b"", filename="notes.txt")
    assert verdict.kind is FileKind.EMPTY
    assert verdict.problem is not None
    assert verdict.problem.code is Code.INPUT_EMPTY


def _all_remedies() -> list[str]:
    remedies: list[str] = []
    for name in dir(sniff_module):
        if name.startswith("_REMEDIES"):
            remedies.extend(getattr(sniff_module, name))
    assert remedies
    return remedies


@pytest.mark.parametrize("forbidden", ["rename", "renaming", ".txt instead of"])
def test_no_remedy_suggests_that_renaming_the_file_would_help(forbidden):
    assert not [r for r in _all_remedies() if forbidden in r.lower()]


@pytest.mark.parametrize("forbidden", ["online", "upload", "internet", "cloud", "website"])
def test_no_remedy_offers_an_external_conversion_service(forbidden):
    assert not [r for r in _all_remedies() if forbidden in r.lower()]


@pytest.mark.parametrize(
    "forbidden",
    ["import socket", "import urllib", "import http", "import requests", "import httpx",
     "urlopen", "http://", "https://", "subprocess"],
)
def test_the_module_contains_no_network_or_execution_code(forbidden):
    source = (sniff_module.__file__ and open(sniff_module.__file__, encoding="utf-8").read()) or ""
    assert forbidden not in source


def test_every_refusal_names_the_supported_formats_and_at_least_one_remedy():
    samples = [
        pdf_bytes(),
        zip_bytes({"word/document.xml": b"<w:document/>"}),
        ole_bytes("WordDocument".encode("utf-16-le")),
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
        pe_bytes(),
        b"RIFF\x24\x00\x00\x00WAVEfmt ",
        b"text then \x00 a nul",
        "café".encode("latin-1"),
        b"",
    ]
    for data in samples:
        verdict = sniff(data, filename="sample.txt")
        assert verdict.problem is not None, verdict
        assert verdict.problem.remedies
        assert verdict.to_dict()["supported_formats"] == ["TXT", "Markdown"]
        assert verdict.as_error().detail["supported_formats"] == ["TXT", "Markdown"]


def test_a_verdict_payload_never_carries_the_file_name_or_a_path():
    verdict = sniff(pdf_bytes(), filename="C:/Users/someone/secret-plans.pdf")
    payload = repr(verdict.to_dict()) + repr(verdict.as_error().to_payload("req_1"))
    assert "secret-plans" not in payload
    assert "Users" not in payload
