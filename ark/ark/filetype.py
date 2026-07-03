"""File-type detection: extension first, magic-byte sniffing as a fallback/check.

Extensions lie (a `.jpg` that's really a PNG, an extensionless camera dump), so
we sniff the leading bytes to recover the true kind when the extension is
missing or unknown.
"""

from __future__ import annotations

from pathlib import Path

from .constants import EXT_MAP, KIND_IMAGE, KIND_VIDEO, KIND_DOCUMENT, KIND_OTHER

# (magic prefix, offset, kind, canonical ext)
_MAGIC: list[tuple[bytes, int, str, str]] = [
    (b"\xff\xd8\xff", 0, KIND_IMAGE, "jpg"),
    (b"\x89PNG\r\n\x1a\n", 0, KIND_IMAGE, "png"),
    (b"GIF87a", 0, KIND_IMAGE, "gif"),
    (b"GIF89a", 0, KIND_IMAGE, "gif"),
    (b"BM", 0, KIND_IMAGE, "bmp"),
    (b"RIFF", 0, KIND_IMAGE, "webp"),   # WEBP also RIFF; refined below
    (b"II*\x00", 0, KIND_IMAGE, "tif"),
    (b"MM\x00*", 0, KIND_IMAGE, "tif"),
    (b"ftypheic", 4, KIND_IMAGE, "heic"),
    (b"ftypheix", 4, KIND_IMAGE, "heic"),
    (b"ftypmif1", 4, KIND_IMAGE, "heic"),
    (b"%PDF", 0, KIND_DOCUMENT, "pdf"),
    (b"ftypqt", 4, KIND_VIDEO, "mov"),
    (b"ftypisom", 4, KIND_VIDEO, "mp4"),
    (b"ftypmp4", 4, KIND_VIDEO, "mp4"),
    (b"ftypM4V", 4, KIND_VIDEO, "m4v"),
    (b"\x1aE\xdf\xa3", 0, KIND_VIDEO, "mkv"),
]


def detect(path: str | Path) -> tuple[str, str, str]:
    """Return ``(ext, kind, mime)`` for ``path``.

    Trusts a known extension; otherwise sniffs magic bytes; otherwise ``other``.
    """
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    if ext in EXT_MAP:
        kind, mime = EXT_MAP[ext]
        return ext, kind, mime

    sniffed = _sniff(p)
    if sniffed:
        s_ext = sniffed
        kind, mime = EXT_MAP.get(s_ext, (KIND_OTHER, "application/octet-stream"))
        return (ext or s_ext), kind, mime

    return ext, KIND_OTHER, "application/octet-stream"


def _sniff(p: Path) -> str | None:
    try:
        with open(p, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    for magic, off, _kind, ext in _MAGIC:
        if head[off:off + len(magic)] == magic:
            # RIFF disambiguation: WEBP has "WEBP" at offset 8
            if magic == b"RIFF" and head[8:12] != b"WEBP":
                continue
            return ext
    return None
