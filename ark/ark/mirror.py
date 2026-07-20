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

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .constants import DIR_OBJECTS
from .hashing import hash_file, verify_copy

_FILESYSTEM_KINDS = ("local", "external", "nas")
MIRROR_MARKER = ".ark-mirror"
_MARKER_FORMAT = 1


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

    def __init__(self, root: Optional[Path], kind: str = "local",
                 vault: Optional[Path] = None):
        self.root = root
        self.kind = kind
        self.vault = vault
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
        # Keep the lexical target so a configured symlink can be detected and
        # rejected; resolve only for the vault relationship check.
        target = Path(os.path.abspath(Path(bt.path).expanduser()))
        if target.resolve() == vault:       # default "backup == vault" means no mirror configured
            return cls(None, bt.kind)
        return cls(target, bt.kind, vault)

    def availability_problem(self, vault: Optional[Path] = None) -> str:
        """Why the configured target is not a real off-site destination.

        This is checked immediately before every write, not just at startup: if
        an external drive disappears and leaves a stale mountpoint behind, ARK
        must not create a local directory there and call it redundancy.
        """
        if not self.enabled:
            return ""
        if vault is None and self.vault is None:
            return "primary vault identity is unavailable"
        primary = Path(vault or self.vault).resolve()
        return self._target_problem(primary, require_marker=True)

    def initialize(self, vault: Optional[Path] = None) -> str:
        """Explicitly bootstrap a fresh off-site target.

        Returns an empty string on success or a human-readable safety problem.
        A missing target directory is created only when its nearest existing
        parent is already on a different device from the primary vault. This
        prevents ``--init`` itself from fabricating a stale mountpoint locally.
        """
        if not self.enabled:
            return "no filesystem mirror target is configured"
        if vault is None and self.vault is None:
            return "primary vault identity is unavailable"
        primary = Path(vault or self.vault).resolve()
        root = self.root

        if root.is_symlink():
            return "mirror root is a symlink"
        if not root.exists():
            ancestor = root.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                if ancestor.is_symlink():
                    return f"mirror path contains symlink: {ancestor}"
                ancestor = ancestor.parent
            if ancestor.is_symlink():
                return f"mirror path contains symlink: {ancestor}"
            if not ancestor.is_dir():
                return "mirror target parent is not a directory"
            try:
                prospective = ancestor.resolve(strict=True) / root.relative_to(ancestor)
                problem = _relationship_problem(primary, prospective)
                if problem:
                    return problem
                if _same_device(primary, ancestor.resolve(strict=True)):
                    return "mirror root would be on the same device as the primary vault"
                root.mkdir(parents=True, exist_ok=False)
            except (OSError, ValueError) as e:
                return f"could not create mirror root safely: {e}"

        problem = self._target_problem(primary, require_marker=False)
        if problem:
            return problem

        marker = root / MIRROR_MARKER
        if marker.is_symlink():
            return "mirror marker is a symlink"
        if marker.exists():
            return self._marker_problem(primary)

        tmp = root / f".tmp-{os.getpid()}-{uuid.uuid4().hex}-{MIRROR_MARKER}"
        payload = {"format": _MARKER_FORMAT, "vault": str(primary)}
        try:
            with open(tmp, "x", encoding="utf-8") as f:
                json.dump(payload, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, marker)
            try:
                dfd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return f"could not create mirror marker: {e}"
        return self._marker_problem(primary)

    def _target_problem(self, primary: Path, *, require_marker: bool) -> str:
        root = self.root
        if root.is_symlink():
            return "mirror root is a symlink"
        if not root.exists():
            return "mirror root is absent (drive/NAS may be disconnected)"
        if not root.is_dir():
            return "mirror root is not a directory"
        try:
            resolved = root.resolve(strict=True)
            problem = _relationship_problem(primary, resolved)
            if problem:
                return problem
            if require_marker:
                problem = self._marker_problem(primary)
                if problem:
                    return problem
            if _same_device(primary, resolved):
                return "mirror root is on the same device as the primary vault"
        except OSError as e:
            return f"mirror root is unreachable: {e}"

        objects = root / DIR_OBJECTS
        if objects.is_symlink():
            return "mirror objects/ is a symlink"
        if objects.exists():
            try:
                objects.resolve(strict=True).relative_to(resolved)
            except (OSError, ValueError):
                return "mirror objects/ escapes the mirror root"
        return ""

    def _marker_problem(self, primary: Path) -> str:
        marker = self.root / MIRROR_MARKER
        if marker.is_symlink():
            return "mirror marker is a symlink"
        if not marker.is_file():
            return "mirror marker missing (run `ark mirror --init` while the target is connected)"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            return f"mirror marker is unreadable or invalid: {e}"
        if not isinstance(payload, dict):
            return "mirror marker is invalid"
        if payload.get("format") != _MARKER_FORMAT:
            return "mirror marker has an unsupported format"
        if payload.get("vault") != str(primary):
            return "mirror marker belongs to a different primary vault"
        return ""

    # ---- replication ------------------------------------------------------
    def replicate_object(self, vault: Path, object_relpath: str, expected_hash: str) -> str:
        """Copy one object from the vault to the mirror (idempotent, verified).

        Returns 'replicated' | 'present' | 'missing' | 'unreachable' | 'corrupt'.
        Never raises — mirroring must not break a scan."""
        if not self.enabled:
            return "present"
        problem = self.availability_problem(Path(vault))
        if problem:
            return f"unavailable ({problem})"
        try:
            src = self._safe_source(Path(vault), object_relpath, expected_hash)
            dst = self._safe_destination(object_relpath, expected_hash)
            if not src.is_file():
                return "missing"
            if dst.is_file() and hash_file(dst) == expected_hash:
                return "present"            # already mirrored and intact
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir so a symlink swap cannot redirect the copy.
            dst = self._safe_destination(object_relpath, expected_hash)
            if not self._atomic_verified_copy(src, dst, expected_hash):
                return "corrupt"
            return "replicated"
        except (OSError, ValueError) as e:
            return f"unavailable ({e})"      # soft failure; primary remains authoritative

    def sync(self, vault: str | Path, object_relpaths) -> MirrorStats:
        """Catch the mirror up: ensure every given object is present + verified.

        ``object_relpaths`` is an iterable of ``(object_relpath, hash)``."""
        vault = Path(vault)
        st = MirrorStats()
        if not self.enabled:
            return st
        objects = list(object_relpaths)
        availability = self.availability_problem(vault)
        if availability:
            # Preserve per-object failure counts for scan summaries, but still
            # make an empty-vault sync fail visibly instead of claiming that an
            # absent/uninitialized target is healthy.
            st.failed = len(objects) or 1
            st.problems.append(availability)
            return st
        for rel, h in objects:
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
        availability = self.availability_problem()
        if availability:
            return [availability]
        for rel, h in object_relpaths:
            try:
                dst = self._safe_destination(rel, h)
                if not dst.is_file():
                    problems.append(f"{rel}: not on mirror")
                elif hash_file(dst) != h:
                    problems.append(f"{rel}: mirror copy corrupt (hash mismatch)")
            except (OSError, ValueError) as e:
                problems.append(f"{rel}: mirror unsafe/unreachable ({e})")
        return problems

    # ---- internals --------------------------------------------------------
    def _atomic_verified_copy(self, src: Path, dst: Path, expected_hash: str) -> bool:
        tmp = dst.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex}-{dst.name}"
        self._assert_within_root(tmp)
        try:
            with open(src, "rb", buffering=0) as fs, open(tmp, "xb", buffering=0) as fd:
                while chunk := fs.read(1 << 20):
                    fd.write(chunk)
                fd.flush()
                os.fsync(fd.fileno())
            ok, _ = verify_copy(src, tmp, expected=expected_hash)
            if not ok:
                tmp.unlink(missing_ok=True)
                return False
            # Destination and temp containment are proven again immediately
            # before replacement. Existing symlinks are never followed.
            self._assert_within_root(tmp)
            self._assert_within_root(dst)
            if dst.is_symlink():
                raise ValueError("mirror destination is a symlink")
            os.replace(tmp, dst)            # atomic within the mirror filesystem
            return True
        except (OSError, ValueError):
            tmp.unlink(missing_ok=True)
            raise

    def _safe_source(self, vault: Path, rel: str, expected_hash: str) -> Path:
        relpath = _validated_object_relpath(rel, expected_hash)
        objects = vault.resolve() / DIR_OBJECTS
        if objects.is_symlink():
            raise ValueError("primary objects/ is a symlink")
        src = vault.resolve() / relpath
        try:
            src.resolve(strict=False).relative_to(objects.resolve(strict=False))
        except ValueError as e:
            raise ValueError("source object escapes primary objects/") from e
        if src.is_symlink():
            raise ValueError("source object is a symlink")
        return src

    def _safe_destination(self, rel: str, expected_hash: str) -> Path:
        relpath = _validated_object_relpath(rel, expected_hash)
        dst = self.root / relpath
        self._assert_within_root(dst)
        if dst.is_symlink():
            raise ValueError("mirror destination is a symlink")
        return dst

    def _assert_within_root(self, target: Path) -> None:
        root = self.root.resolve(strict=True)
        current = target.parent
        while current != self.root.parent and current != self.root:
            if current.exists() and current.is_symlink():
                raise ValueError(f"mirror path contains symlink: {current}")
            current = current.parent
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as e:
            raise ValueError(f"mirror path escapes target root: {target}") from e


def discover_vault_objects(vault: str | Path, db_objects=()) -> list[tuple[str, str]]:
    """Merge DB-known objects with every durable file in the real objects/ tree.

    A process may die after an atomic object rename but before its DB commit.
    Those orphan bytes are still valuable backups and must be mirrored/audited,
    not hidden merely because metadata never caught up.
    """
    vault = Path(vault).resolve()
    found: dict[str, str] = {str(rel): str(h) for rel, h in db_objects}
    objects = vault / DIR_OBJECTS
    if objects.exists() and not objects.is_symlink():
        for obj in objects.rglob("*"):
            if obj.name.startswith(".tmp-"):
                continue
            if not obj.is_file() and not obj.is_symlink():
                continue
            rel = str(obj.relative_to(vault))
            expected = obj.name.split(".", 1)[0]
            found.setdefault(rel, expected)
    return sorted(found.items())


def _validated_object_relpath(rel: str, expected_hash: str) -> Path:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != DIR_OBJECTS:
        raise ValueError(f"invalid object path: {rel}")
    named_hash = path.name.split(".", 1)[0]
    if named_hash != expected_hash:
        raise ValueError(f"object path/hash mismatch: {rel}")
    return path


def _same_device(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def _relationship_problem(primary: Path, target: Path) -> str:
    if target == primary:
        return "mirror root is the primary vault"
    try:
        target.relative_to(primary)
        return "mirror root is inside the primary vault"
    except ValueError:
        pass
    try:
        primary.relative_to(target)
        return "mirror root contains the primary vault"
    except ValueError:
        return ""
