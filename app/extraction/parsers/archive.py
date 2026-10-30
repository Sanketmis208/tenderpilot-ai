"""Safe ZIP extraction: path traversal, symlink, zip-bomb and nesting protections."""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.config import get_settings
from app.core.errors import ValidationFailed

MAX_COMPRESSION_RATIO = 100


@dataclass
class ArchiveMember:
    name: str
    data: bytes


def _is_unsafe(name: str) -> bool:
    p = PurePosixPath(name.replace("\\", "/"))
    return p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":")


def extract_zip(data: bytes) -> tuple[list[ArchiveMember], list[str]]:
    """Return supported members and a list of skipped-member warnings. Raises on dangerous archives."""
    s = get_settings()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValidationFailed("The ZIP archive is corrupted.")
    members, warnings = [], []
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > s.max_zip_members:
        raise ValidationFailed(f"Archive has too many files (max {s.max_zip_members}).")
    total = 0
    for info in infos:
        if _is_unsafe(info.filename):
            raise ValidationFailed(f"Archive contains an unsafe path: {info.filename!r}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValidationFailed("Archive contains symbolic links, which are not allowed.")
        if info.flag_bits & 0x1:
            warnings.append(f"Skipped encrypted file {info.filename}")
            continue
        if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            raise ValidationFailed("Archive compression ratio is suspicious (possible zip bomb).")
        total += info.file_size
        if total > s.max_zip_uncompressed_bytes:
            raise ValidationFailed("Archive expands beyond the permitted size.")
        base = PurePosixPath(info.filename).name
        if base.startswith(".") or base.startswith("__MACOSX"):
            continue
        if base.lower().endswith(".zip"):
            warnings.append(f"Skipped nested archive {info.filename}")
            continue
        with zf.open(info) as fh:
            content = fh.read(info.file_size + 1)
        if len(content) > info.file_size:
            raise ValidationFailed("Archive member size mismatch.")
        members.append(ArchiveMember(info.filename, content))
    return members, warnings
