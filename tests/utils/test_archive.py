from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from nanobot.utils.archive import (
    describe_archive,
    extract_zip_archive,
    is_archive_file,
    unpacked_dir_for,
)


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return path


def _inner_zip_bytes() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("deep.txt", b"deep")
    return buf.getvalue()


# -- extraction ---------------------------------------------------------------


def test_extracts_archive_members_into_sibling_dir(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "data.zip", {"a.txt": b"aaa", "sub/b.csv": b"x,y\n1,2"})

    result = extract_zip_archive(z)

    assert result.extracted_dir == unpacked_dir_for(z)
    assert result.total_files == 2
    assert result.extracted_files == 2
    assert result.skipped == 0
    assert result.truncated is False
    assert (result.extracted_dir / "a.txt").read_bytes() == b"aaa"
    assert (result.extracted_dir / "sub" / "b.csv").read_bytes() == b"x,y\n1,2"
    # The archive itself stays in place.
    assert z.is_file()


def test_reentry_is_idempotent(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "data.zip", {"a.txt": b"aaa"})

    first = extract_zip_archive(z)
    second = extract_zip_archive(z)

    assert second.extracted_dir == first.extracted_dir
    assert second.extracted_files == 1
    assert (second.extracted_dir / "a.txt").read_bytes() == b"aaa"


def test_path_traversal_and_absolute_members_are_skipped(tmp_path: Path) -> None:
    z = _make_zip(
        tmp_path / "evil.zip",
        {
            "../escape.txt": b"boom",
            "/abs.txt": b"boom",
            "C:\\evil.txt": b"boom",
            "ok.txt": b"good",
        },
    )

    result = extract_zip_archive(z)

    assert result.extracted_files == 1
    assert result.skipped == 3
    assert (result.extracted_dir / "ok.txt").read_bytes() == b"good"
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "abs.txt").exists()
    assert not (result.extracted_dir / "C:").exists()


def test_symlink_members_are_skipped(tmp_path: Path) -> None:
    z = tmp_path / "link.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        zf.writestr(info, "/etc/passwd")
        zf.writestr("ok.txt", b"good")

    result = extract_zip_archive(z)

    assert result.extracted_files == 1
    assert result.skipped == 1
    assert not (result.extracted_dir / "evil-link").exists()


def test_encrypted_members_are_skipped(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "enc.zip", {"secret.txt": b"data", "ok.txt": b"good"})
    # ``zipfile`` clears the encryption bit on write, so flip it in the
    # central-directory header bytes directly (sig + ver_needed + flag).
    raw = bytearray(z.read_bytes())
    idx = raw.find(b"PK\x01\x02")
    assert idx != -1
    raw[idx + 8] |= 0x01
    z.write_bytes(bytes(raw))

    result = extract_zip_archive(z)

    assert result.extracted_files == 1
    assert result.skipped == 1
    assert not (result.extracted_dir / "secret.txt").exists()
    assert (result.extracted_dir / "ok.txt").read_bytes() == b"good"


def test_member_count_cap_stops_with_truncated_flag(tmp_path: Path) -> None:
    z = _make_zip(
        tmp_path / "many.zip", {"1.txt": b"a", "2.txt": b"b", "3.txt": b"c"}
    )

    result = extract_zip_archive(z, max_files=1)

    assert result.total_files == 3
    assert result.extracted_files == 1
    assert result.truncated is True


def test_unpack_byte_budget_stops_with_truncated_flag(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "big.zip", {"1.bin": b"aaa", "2.bin": b"bb"})

    result = extract_zip_archive(z, max_unpack_bytes=4)

    assert result.extracted_files == 1
    assert result.truncated is True
    assert (result.extracted_dir / "1.bin").read_bytes() == b"aaa"
    # The member that would exceed the budget leaves no partial file behind.
    assert not (result.extracted_dir / "2.bin").exists()


def test_nested_zip_is_stored_but_not_recursed(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "outer.zip", {"inner.zip": _inner_zip_bytes()})

    result = extract_zip_archive(z)

    assert result.extracted_files == 1
    inner = result.extracted_dir / "inner.zip"
    assert inner.is_file()
    assert not unpacked_dir_for(inner).exists()


def test_corrupt_archive_degrades_to_unextracted(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip archive")

    assert is_archive_file(junk) is False
    assert extract_zip_archive(junk).extracted_dir is None
    assert describe_archive(junk) is None


def test_office_container_with_docx_suffix_is_not_an_archive(tmp_path: Path) -> None:
    docx = _make_zip(tmp_path / "doc.docx", {"[Content_Types].xml": b"<xml/>"})

    assert is_archive_file(docx) is False
    assert describe_archive(docx) is None


# -- manifest -----------------------------------------------------------------


def test_describe_archive_manifest_after_extraction(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "data.zip", {"a.txt": b"aaa", "sub/b.csv": b"x"})

    extract_zip_archive(z)
    block = describe_archive(z)

    assert block is not None
    assert block.startswith(f"[Archive: {z}")
    assert str(unpacked_dir_for(z)) in block
    assert "2 files, extracted" in block
    assert "  - a.txt" in block
    assert "  - sub/b.csv" in block


def test_describe_archive_caps_listing_with_more_marker(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(5)})

    extract_zip_archive(z)
    block = describe_archive(z, max_entries=2)

    assert block is not None
    assert "… +3 more" in block
    assert "  - f0.txt" in block
    assert "  - f4.txt" not in block


def test_describe_archive_manifest_without_extraction(tmp_path: Path) -> None:
    z = _make_zip(tmp_path / "data.zip", {"a.txt": b"aaa"})

    block = describe_archive(z)

    assert block is not None
    assert block.startswith(f"[Archive: {z}]")
    assert "not unpacked" in block
    assert "  - a.txt" in block
