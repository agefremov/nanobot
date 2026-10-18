"""Safe, bounded unpacking of uploaded ZIP archives.

Uploaded ``.zip`` archives are stored like any other document and then
unpacked into a sibling directory (``<name>.unpacked/``) so the model can
read individual members with ``read_file`` without going through the shell.

A ZIP archive is untrusted input, so extraction enforces:

- **no path traversal** — member names are sanitized (``..``, absolute
  paths, drive prefixes, NUL bytes are rejected) and symlink members are
  skipped;
- **bounded work** — at most :data:`MAX_ARCHIVE_FILES` members and
  :data:`MAX_ARCHIVE_UNPACKED_BYTES` of *actual* decompressed bytes
  (declared sizes in headers are not trusted); extraction stops early and
  the manifest reports the partial result;
- **no recursion** — nested archives are stored as plain files and never
  unpacked in turn;
- **no encrypted members** — flagged entries are skipped.

Nothing here ever raises on hostile input: a broken archive degrades to an
unextracted file plus a ``[Archive: ...]`` manifest reference.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 50

_CHUNK = 64 * 1024
_ZIP_SUFFIXES = frozenset({".zip"})
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ArchiveExtraction:
    """Outcome of one :func:`extract_zip_archive` call."""

    extracted_dir: Path | None
    total_files: int
    extracted_files: int
    skipped: int
    truncated: bool


def unpacked_dir_for(archive_path: str | Path) -> Path:
    """Deterministic sibling directory holding an archive's contents."""
    return Path(str(archive_path) + ".unpacked")


def is_archive_file(path: str | Path) -> bool:
    """True only for real ZIP archives with a ``.zip`` suffix.

    The suffix gate matters: ``.docx``/``.xlsx``/``.pptx`` files are ZIP
    containers too and must not be exploded into their XML parts.
    """
    p = Path(path)
    if p.suffix.lower() not in _ZIP_SUFFIXES or not p.is_file():
        return False
    try:
        return zipfile.is_zipfile(p)
    except OSError:
        return False


def _safe_member_parts(name: str) -> tuple[str, ...] | None:
    """Sanitize a member name into safe relative parts, or ``None``."""
    normalized = name.replace("\\", "/")
    if not normalized or "\x00" in normalized or _WIN_DRIVE_RE.match(normalized):
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return None
    parts = tuple(part for part in pure.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        return None
    # ``parts`` are free of ``..`` and not absolute, so joinpath cannot
    # escape the target directory (symlink members are skipped below).
    return parts


def extract_zip_archive(
    path: str | Path,
    *,
    max_files: int = MAX_ARCHIVE_FILES,
    max_unpack_bytes: int = MAX_ARCHIVE_UNPACKED_BYTES,
) -> ArchiveExtraction:
    """Unpack *path* into :func:`unpacked_dir_for` under strict bounds.

    Idempotent: when the unpacked directory already exists the archive is
    not touched again. Hostile or corrupt members are skipped; when a limit
    is hit extraction stops and ``truncated`` is set. On unexpected errors
    the partial directory is removed and the archive is reported as
    unextracted.
    """
    p = Path(path)
    target = unpacked_dir_for(p)
    if target.is_dir():
        count = sum(1 for f in target.rglob("*") if f.is_file())
        return ArchiveExtraction(target, count, count, 0, False)

    try:
        zf = zipfile.ZipFile(p)
    except (OSError, zipfile.BadZipFile):
        return ArchiveExtraction(None, 0, 0, 0, False)

    extracted = 0
    skipped = 0
    truncated = False
    written = 0
    try:
        with zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            total = len(members)
            target.mkdir(parents=True, exist_ok=True)
            try:
                for info in members:
                    if extracted >= max_files or written >= max_unpack_bytes:
                        truncated = True
                        break
                    if info.flag_bits & 0x1:  # encrypted
                        skipped += 1
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16):
                        skipped += 1
                        continue
                    parts = _safe_member_parts(info.filename)
                    if parts is None:
                        skipped += 1
                        continue
                    dest = target.joinpath(*parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zf.open(info) as src, dest.open("wb") as out:
                            while True:
                                chunk = src.read(_CHUNK)
                                if not chunk:
                                    break
                                if written + len(chunk) > max_unpack_bytes:
                                    truncated = True
                                    break
                                out.write(chunk)
                                written += len(chunk)
                    except (
                        OSError,
                        RuntimeError,
                        ValueError,
                        NotImplementedError,
                        zipfile.BadZipFile,
                    ):
                        dest.unlink(missing_ok=True)
                        skipped += 1
                        continue
                    if truncated:
                        dest.unlink(missing_ok=True)
                        break
                    extracted += 1
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
    except (OSError, zipfile.BadZipFile):
        shutil.rmtree(target, ignore_errors=True)
        return ArchiveExtraction(None, 0, 0, 0, False)
    return ArchiveExtraction(target, total, extracted, skipped, truncated)


def describe_archive(
    path: str | Path, *, max_entries: int = MAX_MANIFEST_ENTRIES
) -> str | None:
    """Return a manifest block for an archive, else ``None``.

    Extracted archives list the files actually on disk (so the model knows
    what it can read); unextracted ones list member names and point at
    ``exec`` for on-demand unpacking. Listings are capped at *max_entries*.
    """
    p = Path(path)
    if not is_archive_file(p):
        return None
    target = unpacked_dir_for(p)

    names: list[str] = []
    try:
        with zipfile.ZipFile(p) as zf:
            names = [
                i.filename.replace("\\", "/")
                for i in zf.infolist()
                if not i.is_dir()
            ]
    except (OSError, zipfile.BadZipFile):
        names = []

    if target.is_dir():
        entries = sorted(
            str(f.relative_to(target)) for f in target.rglob("*") if f.is_file()
        )
        total = max(len(names), len(entries))
        if not entries:
            status = f"{total} files, empty"
        elif len(entries) < total:
            status = f"{len(entries)} of {total} files extracted, rest skipped"
        else:
            status = f"{len(entries)} files, extracted"
        header = f"[Archive: {p} -> {target}/] ({status})"
    else:
        entries = names
        status = (
            f"{len(entries)} files, not unpacked — extract with exec if needed"
        )
        header = f"[Archive: {p}] ({status})"

    lines = [header]
    lines.extend(f"  - {name}" for name in entries[:max_entries])
    if len(entries) > max_entries:
        lines.append(f"  … +{len(entries) - max_entries} more")
    return "\n".join(lines)
