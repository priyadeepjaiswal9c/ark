"""Off-site mirror — a second, verified copy of the object store.

The vault's own directory is your primary backup. But a backup that lives on one
disk is one disk failure from gone. The ``[backup]`` config target, when it
points somewhere *other* than the vault (an external SSD, a NAS mount), becomes a
live **mirror**: every content-addressed object is additionally replicated there,
atomically and hash-verified, so your memories survive losing either location.

Guarantees, in keeping with the sacred rule:
  * The mirror only ever *gains* objects (append-only, content-addressed). It is
    never the authority and is never deleted from here.
  * Each mirrored object is written to a temp file, fsync'd, re-hashed, and only
    then atomically renamed — a torn copy never masquerades as a good one.
  * If the mirror is unreachable (drive unplugged, NAS down), replication fails
    *soft*: the scan still succeeds against the primary vault, and ``ark mirror``
    catches the mirror up later. A mirror is redundancy, never a dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .constants import DIR_OBJECTS
from .hashing import hash_file, verify_copy

_FILESYSTEM_KINDS = ("local", "external", "nas")


@dataclass
class MirrorStats:
    replicated: int = 0
    already_present: int = 0
    failed: int = 0
    missing_source: int = 0
    problems: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.failed == 0 and not self.problems


class Mirror:
    """A filesystem mirror of ``objects/`` at a second root. ``enabled`` is False
    when no distinct, supported target is configured, so callers can stay
    unconditional (every method is a cheap no-op when disabled)."""

    def __init__(self, root: Optional[Path], kind: str = "local"):
        self.root = root
        self.kind = kind
        self.objects = (root / DIR_OBJECTS) if root else None

    @property
    def enabled(self) -> bool:
        return self.root is not None

    @classmethod
    def from_config(cls, cfg: Config, vault: str | Path) -> "Mirror":
        vault = Path(vault).resolve()
        bt = cfg.backup
        if not bt.path:
            return cls(None, bt.kind)
        # Cloud needs an object-store adapter we don't ship yet — disable cleanly.
        if bt.kind not in _FILESYSTEM_KINDS:
            return cls(None, bt.kind)
        target = Path(bt.path).expanduser().resolve()
        if target == vault:                 # "backup == the vault itself" -> no separate mirror
            return cls(None, bt.kind)
        return cls(target, bt.kind)

    # ---- replication ------------------------------------------------------
    def replicate_object(self, vault: Path, object_relpath: str, expected_hash: str) -> str:
        """Copy one object from the vault to the mirror (idempotent, verified).

        Returns 'replicated' | 'present' | 'missing' | 'unreachable' | 'corrupt'.
        Never raises — mirroring must not break a scan."""
        if not self.enabled:
            return "present"
        src = Path(vault) / object_relpath
        dst = self.root / object_relpath
        try:
            if not src.is_file():
                return "missing"
            if dst.is_file() and hash_file(dst) == expected_hash:
                return "present"            # already mirrored and intact
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not self._atomic_verified_copy(src, dst, expected_hash):
                return "corrupt"
            return "replicated"
        except OSError:
            return "unreachable"            # drive unplugged / NAS down — soft fail

    def sync(self, vault: str | Path, object_relpaths) -> MirrorStats:
        """Catch the mirror up: ensure every given object is present + verified.

        ``object_relpaths`` is an iterable of ``(object_relpath, hash)``."""
        vault = Path(vault)
        st = MirrorStats()
        if not self.enabled:
            return st
        for rel, h in object_relpaths:
            outcome = self.replicate_object(vault, rel, h)
            if outcome == "replicated":
                st.replicated += 1
            elif outcome == "present":
                st.already_present += 1
            elif outcome == "missing":
                st.missing_source += 1
                st.problems.append(f"{rel}: source object missing in vault")
            else:
                st.failed += 1
                st.problems.append(f"{rel}: {outcome}")
        return st

    def verify(self, object_relpaths) -> list[str]:
        """Re-hash each object at the mirror; report anything missing or wrong."""
        problems: list[str] = []
        if not self.enabled:
            return problems
        for rel, h in object_relpaths:
            dst = self.root / rel
            try:
                if not dst.is_file():
                    problems.append(f"{rel}: not on mirror")
                elif hash_file(dst) != h:
                    problems.append(f"{rel}: mirror copy corrupt (hash mismatch)")
            except OSError:
                problems.append(f"{rel}: mirror unreachable")
        return problems

    # ---- internals --------------------------------------------------------
    def _atomic_verified_copy(self, src: Path, dst: Path, expected_hash: str) -> bool:
        tmp = dst.parent / f".tmp-{os.getpid()}-{dst.name}"
        try:
            with open(src, "rb", buffering=0) as fs, open(tmp, "wb", buffering=0) as fd:
                while chunk := fs.read(1 << 20):
                    fd.write(chunk)
                fd.flush()
                os.fsync(fd.fileno())
            ok, _ = verify_copy(src, tmp, expected=expected_hash)
            if not ok:
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, dst)            # atomic within the mirror filesystem
            return True
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
