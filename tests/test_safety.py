import zipfile
from pathlib import Path

import pytest

from classbot.safety import (
    UnsupportedFileError,
    detect_file_type,
    is_within,
    safe_filename,
    sha256_file,
    validate_file,
)


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\windows\\system32.pdf", "system32.pdf"),
    ("/abs/path/第3回.pdf", "第3回.pdf"),
    (".hidden.md", "hidden.md"),
    ("a\x00b\x1fc.txt", "abc.txt"),
    ('bad:*?"<>|.pdf', "bad_______.pdf"),
    ("..", "file"),
    ("", "file"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_safe_filename_length_limit_keeps_extension():
    n = safe_filename("あ" * 300 + ".pdf")
    assert n.endswith(".pdf")
    assert len(n.encode("utf-8")) <= 200


def test_detect_file_type():
    assert detect_file_type("x.PDF") == "pdf"
    assert detect_file_type("x.markdown") == "md"
    assert detect_file_type("x.pptx") == "pptx"
    assert detect_file_type("x.exe") is None
    assert detect_file_type("x.zip") is None


def test_is_within(tmp_path: Path):
    base = tmp_path / "materials"
    base.mkdir()
    (base / "a.txt").write_text("x")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    link = base / "link.txt"
    link.symlink_to(outside)
    assert is_within(base, base / "a.txt")
    assert not is_within(base, base / ".." / "secret.txt")
    assert not is_within(base, link)


def test_sha256(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_validate_bad_magic(tmp_path: Path, settings):
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"MZ\x90\x00 not a pdf")
    with pytest.raises(UnsupportedFileError, match="PDF"):
        validate_file(p, "pdf", settings)
    q = tmp_path / "fake.docx"
    q.write_bytes(b"%PDF-1.4 not a zip")
    with pytest.raises(UnsupportedFileError, match="DOCX"):
        validate_file(q, "docx", settings)


def test_validate_empty_and_size(tmp_path: Path, settings):
    p = tmp_path / "e.txt"
    p.write_bytes(b"")
    with pytest.raises(UnsupportedFileError, match="空"):
        validate_file(p, "txt", settings)
    settings.max_file_size_mb = 1
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (1024 * 1024 + 1))
    with pytest.raises(UnsupportedFileError, match="上限"):
        validate_file(big, "txt", settings)


def test_validate_binary_text(tmp_path: Path, settings):
    p = tmp_path / "b.txt"
    p.write_bytes(b"abc\x00def")
    with pytest.raises(UnsupportedFileError):
        validate_file(p, "txt", settings)


def test_validate_unsupported_type(tmp_path: Path, settings):
    p = tmp_path / "x.exe"
    p.write_bytes(b"MZ")
    with pytest.raises(UnsupportedFileError, match="未対応"):
        validate_file(p, "exe", settings)


def test_zip_bomb_rejected(tmp_path: Path, settings):
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("word/document.xml", "<x/>")
        zf.writestr("word/huge.bin", b"\0" * (50 * 1024 * 1024))
    with pytest.raises(UnsupportedFileError, match="圧縮率|サイズ"):
        validate_file(p, "docx", settings)


def test_zip_too_many_entries(tmp_path: Path, settings):
    settings.zip_max_entries = 10
    p = tmp_path / "many.pptx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("ppt/presentation.xml", "<x/>")
        for i in range(20):
            zf.writestr(f"ppt/f{i}.xml", "x")
    with pytest.raises(UnsupportedFileError, match="要素数"):
        validate_file(p, "pptx", settings)


def test_zip_missing_required_part(tmp_path: Path, settings):
    p = tmp_path / "not.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("hello.txt", "x")
    with pytest.raises(UnsupportedFileError, match="正しくありません"):
        validate_file(p, "docx", settings)
