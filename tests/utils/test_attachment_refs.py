from __future__ import annotations

import zipfile
from pathlib import Path

from nanobot.utils.archive import extract_zip_archive, unpacked_dir_for
from nanobot.utils.document import reference_non_image_attachments


def test_plain_document_stays_an_attachment_reference(tmp_path: Path) -> None:
    doc = tmp_path / "report.txt"
    doc.write_text("hello")

    content, images = reference_non_image_attachments("analyze", [str(doc)])

    assert f"[Attachment: {doc}]" in content
    assert images == []


def test_image_paths_are_split_out_for_vision_blocks(tmp_path: Path) -> None:
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    content, images = reference_non_image_attachments("look", [str(img)])

    assert str(img) in images
    assert "[Attachment:" not in content


def test_extracted_archive_gets_manifest_not_attachment_ref(tmp_path: Path) -> None:
    z = tmp_path / "data.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", b"aaa")
        zf.writestr("sub/b.csv", b"x")
    extract_zip_archive(z)

    content, images = reference_non_image_attachments("unpack this", [str(z)])

    assert f"[Archive: {z}" in content
    assert str(unpacked_dir_for(z)) in content
    assert "  - a.txt" in content
    assert f"[Attachment: {z}]" not in content
    assert images == []


def test_unextracted_archive_manifest_mentions_exec_fallback(tmp_path: Path) -> None:
    z = tmp_path / "data.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", b"aaa")

    content, _ = reference_non_image_attachments("inspect", [str(z)])

    assert f"[Archive: {z}]" in content
    assert "not unpacked" in content
