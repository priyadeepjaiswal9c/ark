"""Content hashing — the identity of every asset in the vault.

The hash *is* the dedup key and the integrity proof. `hash_file` streams the
file so multi-GB videos never blow up memory. `verify_copy` re-hashes a copied
file and confirms it byte-for-byte matches the source — this is the guarantee
behind ARK's "hash-proven safe to delete from your phone" claim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .constants import HASH_ALGO, HASH_DIGEST_SIZE, READ_CHUNK


def _new():
    if HASH_ALGO == "blake2b":
        return hashlib.blake2b(digest_size=HASH_DIGEST_SIZE)
    return hashlib.new(HASH_ALGO)


def hash_file(path: str | Path) -> str:
    """Return the hex content hash of ``path`` (streaming)."""
    h = _new()
    with open(path, "rb", buffering=0) as f:
        while chunk := f.read(READ_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    h = _new()
    h.update(data)
    return h.hexdigest()


def verify_copy(src: str | Path, dst: str | Path, expected: str | None = None) -> tuple[bool, str]:
    """Confirm ``dst`` is a faithful copy of ``src``.

    Compares sizes first (cheap), then hashes ``dst`` and checks it against
    ``expected`` (the source hash we already computed). Returns
    ``(ok, dst_hash)``. Never mutates either file.
    """
    src_p, dst_p = Path(src), Path(dst)
    if not dst_p.exists():
        return False, ""
    if src_p.stat().st_size != dst_p.stat().st_size:
        return False, ""
    dst_hash = hash_file(dst_p)
    if expected is not None:
        return dst_hash == expected, dst_hash
    return dst_hash == hash_file(src_p), dst_hash
