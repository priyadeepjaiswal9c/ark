"""Shared constants: vault layout, file-type maps, status values."""

from __future__ import annotations

# ---- Vault layout (all paths relative to the vault root) -------------------
DIR_OBJECTS = "objects"        # content-addressed canonical store: objects/ab/abcd...ext
DIR_ORGANIZED = "organized"    # human-readable rule-driven tree (hardlinks into objects)
DIR_QUARANTINE = "quarantine"  # dedup / needs-review pointers (never real deletes)
DIR_META = ".ark"              # db, config, logs, run reports
DB_FILENAME = "ark.db"
CONFIG_FILENAME = "ark.toml"
LOG_FILENAME = "ark.log"

# ---- Asset status ----------------------------------------------------------
STATUS_STORED = "stored"          # copied into objects, hash-verified
STATUS_DUPLICATE = "duplicate"    # bytes already in vault (source is redundant)
STATUS_NEEDS_REVIEW = "needs_review"  # stored but missing metadata to organize confidently
STATUS_FAILED = "failed"          # extraction/copy/verify failed — source left untouched

# ---- Coarse file kinds (for rules + reporting) -----------------------------
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_DOCUMENT = "document"
KIND_ARCHIVE = "archive"
KIND_OTHER = "other"

# extension -> (kind, canonical_mime). Lowercase, no dot.
EXT_MAP: dict[str, tuple[str, str]] = {
    # images
    "jpg": (KIND_IMAGE, "image/jpeg"), "jpeg": (KIND_IMAGE, "image/jpeg"),
    "png": (KIND_IMAGE, "image/png"), "gif": (KIND_IMAGE, "image/gif"),
    "heic": (KIND_IMAGE, "image/heic"), "heif": (KIND_IMAGE, "image/heif"),
    "webp": (KIND_IMAGE, "image/webp"), "tif": (KIND_IMAGE, "image/tiff"),
    "tiff": (KIND_IMAGE, "image/tiff"), "bmp": (KIND_IMAGE, "image/bmp"),
    "dng": (KIND_IMAGE, "image/x-adobe-dng"), "raw": (KIND_IMAGE, "image/x-raw"),
    "cr2": (KIND_IMAGE, "image/x-canon-cr2"), "nef": (KIND_IMAGE, "image/x-nikon-nef"),
    # video
    "mp4": (KIND_VIDEO, "video/mp4"), "mov": (KIND_VIDEO, "video/quicktime"),
    "m4v": (KIND_VIDEO, "video/x-m4v"), "avi": (KIND_VIDEO, "video/x-msvideo"),
    "mkv": (KIND_VIDEO, "video/x-matroska"), "webm": (KIND_VIDEO, "video/webm"),
    "3gp": (KIND_VIDEO, "video/3gpp"),
    # audio
    "mp3": (KIND_AUDIO, "audio/mpeg"), "m4a": (KIND_AUDIO, "audio/mp4"),
    "wav": (KIND_AUDIO, "audio/wav"), "flac": (KIND_AUDIO, "audio/flac"),
    "aac": (KIND_AUDIO, "audio/aac"), "ogg": (KIND_AUDIO, "audio/ogg"),
    # documents
    "pdf": (KIND_DOCUMENT, "application/pdf"), "doc": (KIND_DOCUMENT, "application/msword"),
    "docx": (KIND_DOCUMENT, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xls": (KIND_DOCUMENT, "application/vnd.ms-excel"),
    "xlsx": (KIND_DOCUMENT, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "ppt": (KIND_DOCUMENT, "application/vnd.ms-powerpoint"),
    "pptx": (KIND_DOCUMENT, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "txt": (KIND_DOCUMENT, "text/plain"), "md": (KIND_DOCUMENT, "text/markdown"),
    "csv": (KIND_DOCUMENT, "text/csv"), "rtf": (KIND_DOCUMENT, "application/rtf"),
    "pages": (KIND_DOCUMENT, "application/x-iwork-pages-sffpages"),
    # archives
    "zip": (KIND_ARCHIVE, "application/zip"), "tar": (KIND_ARCHIVE, "application/x-tar"),
    "gz": (KIND_ARCHIVE, "application/gzip"), "7z": (KIND_ARCHIVE, "application/x-7z-compressed"),
    "rar": (KIND_ARCHIVE, "application/vnd.rar"),
}

# Files ARK will skip entirely (OS cruft, sidecars handled separately).
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}
IGNORE_PREFIXES = ("._",)  # AppleDouble resource forks

HASH_ALGO = "blake2b"      # fast, cryptographically strong content hash
HASH_DIGEST_SIZE = 32      # 256-bit
READ_CHUNK = 1 << 20       # 1 MiB streaming read
