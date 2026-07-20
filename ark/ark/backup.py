"""The vault writer — where ARK's sacred rule lives.

Guarantees, enforced here and nowhere bypassed:
  1. Sources are READ-ONLY. This module never opens a source for writing and
     never renames/removes/moves a source path. Full stop.
  2. Content-addressed dedup. Every distinct content is stored exactly once at
     ``objects/<h[:2]>/<hash>.<ext>``; re-seeing the same bytes is a no-op copy.
  3. Atomic + verified writes. New objects are written to a temp file, fsync'd,
     re-hashed, and only then atomically renamed into place. A hash mismatch
     deletes the temp and fails the asset — the source is left untouched.
  4. Non-destructive organize. The ``organized/`` tree is hardlinks (or symlinks
     / copies) into ``objects/``; name collisions get a numeric suffix, they
     never overwrite. Versions are recorded, not clobbered.
  5. Everything stays inside the vault (path-traversal proof).
  6. ``dry_run`` performs zero writes but returns the exact plan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .constants import DIR_OBJECTS, DIR_ORGANIZED, DIR_QUARANTINE
from .hashing import hash_file, verify_copy
from .models import Asset
from .rules import RuleMatch


@dataclass
class StoreResult:
    hash: str
    object_relpath: str
    is_new_object: bool
    organized_relpath: str
    logical_path: str
    is_new_version: bool
    verified: bool
    action: str                 # stored | duplicate | healed | failed
    error: str = ""

    @property
    def vault_has_good_copy(self) -> bool:
        """True iff, after this call, a hash-verified object exists for this
        content that pre-dated this source (i.e. the source is genuinely
        redundant). `healed` means the vault copy was NOT good until this source
        rewrote it, so the source is NOT safe to delete."""
        return self.action == "duplicate"


class VaultError(RuntimeError):
    pass


class VaultWriter:
    def __init__(self, vault: str | Path, cfg: Config):
        self.vault = Path(vault).resolve()
        self.cfg = cfg
        self.objects = self.vault / DIR_OBJECTS
        self.organized = self.vault / DIR_ORGANIZED
        self.quarantine = self.vault / DIR_QUARANTINE

    # ---- public API -------------------------------------------------------
    def store(self, asset: Asset, match: RuleMatch, dry_run: bool = False,
              skip_organize: bool = False,
              existing_organized: Optional[str] = None) -> StoreResult:
        """Back up ``asset`` into the vault. When ``skip_organize`` is set the
        object is still stored/verified but no ``organized/`` link is created —
        used on rescan for assets whose organized entry is in quarantine, so a
        scan never silently un-quarantines them."""
        h = asset.hash
        obj_abs = self._object_path(asset)
        obj_rel = str(obj_abs.relative_to(self.vault))
        is_new_object = not obj_abs.exists()

        # Plan the organized destination (collision-safe).
        dest_dir = (self.organized / match.dest_relpath).resolve()
        self._assert_within(self.organized, dest_dir)
        filename = self._safe_filename(Path(asset.source_path).name, asset.ext, h)
        logical_path = f"{match.dest_relpath}/{filename}"
        organized_abs: Optional[Path] = None
        if existing_organized:
            # A rescan may reuse *this source's* existing organized entry. A new
            # duplicate source must never reuse a sibling's path: otherwise
            # quarantining one DB row moves the one shared link out from under
            # every sibling that points at it.
            preferred = (self.vault / existing_organized).resolve()
            self._assert_within(self.organized, preferred)
            try:
                if (not preferred.exists() and not preferred.is_symlink()) or (
                    preferred.is_file() and hash_file(preferred) == h
                ):
                    organized_abs = preferred
            except OSError:
                pass
        if organized_abs is None:
            organized_abs = self._resolve_collision(dest_dir, filename)
        organized_rel = str(organized_abs.relative_to(self.vault))

        if dry_run:
            # In preview we can still cheaply tell whether a *good* copy exists.
            good = (not is_new_object) and self._object_is_valid(obj_abs, h)
            return StoreResult(
                hash=h, object_relpath=obj_rel, is_new_object=is_new_object,
                organized_relpath=organized_rel, logical_path=logical_path,
                is_new_version=is_new_object, verified=good,
                action=("duplicate" if good else "stored"),
            )

        # ---- real writes: the OBJECT (backup) first ----
        try:
            if is_new_object:
                action = "stored"
                if not self._write_object(asset.source_path, obj_abs, h):
                    return self._failed(h, obj_rel, logical_path,
                                        "hash mismatch after copy — source left untouched")
            else:
                # Object already at the content address — but NEVER trust it
                # blindly. Re-verify; if the stored bytes are wrong/short, the
                # vault copy is NOT good, so heal it from the in-hand source.
                if self._object_is_valid(obj_abs, h):
                    action = "duplicate"
                else:
                    # In-place so every hardlinked organized/ view is healed too.
                    if not self._write_object(asset.source_path, obj_abs, h, in_place=True):
                        return self._failed(h, obj_rel, logical_path,
                                            "existing object corrupt and could not be healed")
                    action = "healed"
        except OSError as e:
            return self._failed(h, obj_rel, logical_path, str(e))

        # ---- the organized/ view second: the backup is already durable, so a
        # link failure must NOT discard it. Report success (the object is good
        # and will be registered) but flag the organize gap; a later scan fills it.
        organize_error = ""
        if skip_organize:
            organized_rel = ""      # asset is quarantined — leave organized/ untouched
        else:
            try:
                self._link_into_organized(obj_abs, organized_abs)
            except OSError as e:
                organize_error = f"backed up OK, but organizing failed: {e}"
                organized_rel = ""

        return StoreResult(
            hash=h, object_relpath=obj_rel, is_new_object=(action != "duplicate"),
            organized_relpath=organized_rel, logical_path=logical_path,
            is_new_version=(action != "duplicate"), verified=True, action=action,
            error=organize_error,
        )

    @staticmethod
    def _failed(h: str, obj_rel: str, logical_path: str, error: str) -> StoreResult:
        return StoreResult(
            hash=h, object_relpath=obj_rel, is_new_object=False, organized_relpath="",
            logical_path=logical_path, is_new_version=False, verified=False,
            action="failed", error=error,
        )

    def _object_is_valid(self, obj_abs: Path, expected_hash: str) -> bool:
        """Does the object on disk actually hash to its content address?"""
        try:
            if not obj_abs.is_file():
                return False
            return hash_file(obj_abs) == expected_hash
        except OSError:
            return False

    def verify_vault(self) -> list[dict]:
        """Re-hash every object AND every linked view (organized/ and
        quarantine/); report anything that doesn't prove out. Read-only.

        Each problem is ``{"path": <rel>, "problem": <str>}``. Linked entries
        that are hardlinks to an already-verified object are skipped (same
        inode, same bytes) — so the common case only pays one hash per object.
        """
        problems: list[dict] = []
        if not self.objects.exists():
            return problems

        object_hashes: set[str] = set()
        object_inodes: set[tuple[int, int]] = set()
        for prefix in self.objects.iterdir():
            if not prefix.is_dir():
                continue
            for obj in prefix.iterdir():
                if not obj.is_file():
                    continue
                expected = obj.stem  # filename is <hash>.<ext>
                object_hashes.add(expected)
                try:
                    st = obj.stat()
                    object_inodes.add((st.st_dev, st.st_ino))
                except OSError:
                    pass
                if hash_file(obj) != expected:
                    problems.append({"path": str(obj.relative_to(self.vault)),
                                     "problem": "object does not match its content address"})

        for tree in (self.organized, self.quarantine):
            if not tree.exists():
                continue
            for f in tree.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if (st.st_dev, st.st_ino) in object_inodes:
                    continue  # hardlink to a verified object — already proven
                if hash_file(f) not in object_hashes:
                    problems.append({"path": str(f.relative_to(self.vault)),
                                     "problem": f"{tree.name} entry not backed by any vault object"})
        return problems

    # ---- internals --------------------------------------------------------
    def _object_path(self, asset: Asset) -> Path:
        h = asset.hash
        name = f"{h}.{asset.ext}" if asset.ext else h
        p = (self.objects / h[:2] / name).resolve()
        self._assert_within(self.objects, p)
        return p

    def _write_object(self, src: str, dst: Path, expected_hash: str,
                      in_place: bool = False) -> bool:
        """Copy src -> dst and verify. Returns True iff verified. Source is
        opened read-only throughout.

        Two modes:
        - default: write a temp sibling, fsync, re-hash it, then os.replace() —
          atomic, for brand-new objects.
        - in_place=True (healing): overwrite the *existing inode's* content so
          every hardlink that shares it (the object AND all organized/ views) is
          repaired at once. A new inode via os.replace would leave those
          hardlinks pointing at the old, still-corrupt bytes. The heal window is
          non-atomic, but the source is never touched and a re-run re-heals
          idempotently, so nothing is ever lost or falsely called safe.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        if in_place:
            try:
                with open(src, "rb", buffering=0) as fsrc, open(dst, "wb", buffering=0) as fdst:
                    while chunk := fsrc.read(1 << 20):
                        fdst.write(chunk)
                    fdst.flush()
                    os.fsync(fdst.fileno())
                ok, _ = verify_copy(src, dst, expected=expected_hash)
                return ok
            except OSError:
                raise

        tmp = dst.parent / f".tmp-{os.getpid()}-{dst.name}"
        try:
            with open(src, "rb", buffering=0) as fsrc, open(tmp, "wb", buffering=0) as fdst:
                while chunk := fsrc.read(1 << 20):
                    fdst.write(chunk)
                fdst.flush()
                os.fsync(fdst.fileno())
            ok, _ = verify_copy(src, tmp, expected=expected_hash)
            if not ok:
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, dst)   # atomic within the vault filesystem
            return True
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _link_into_organized(self, obj: Path, organized: Path) -> None:
        organized.parent.mkdir(parents=True, exist_ok=True)
        if organized.exists() or organized.is_symlink():
            return  # collision already resolved to a fresh name; nothing to do
        mode = self.cfg.link_mode
        try:
            if mode == "hardlink":
                os.link(obj, organized)
            elif mode == "symlink":
                organized.symlink_to(obj)
            else:  # copy
                self._plain_copy(obj, organized)
        except OSError as e:
            # cross-device or FS without hardlinks -> degrade to copy
            if getattr(e, "errno", None) in (18,) or mode == "hardlink":
                self._plain_copy(obj, organized)
            else:
                raise

    @staticmethod
    def _plain_copy(src: Path, dst: Path) -> None:
        with open(src, "rb", buffering=0) as fs, open(dst, "wb", buffering=0) as fd:
            while chunk := fs.read(1 << 20):
                fd.write(chunk)

    def _resolve_collision(self, dest_dir: Path, filename: str) -> Path:
        """Return a path in dest_dir that doesn't clobber a *different* file.

        Always give a newly-seen source its own organized entry. Idempotent
        rescans reuse ``existing_organized`` in ``store``; content equality
        alone is insufficient because multiple source rows need independent
        links for consistent quarantine/undo behavior.
        """
        stem, dot, ext = filename.partition(".")
        candidate = dest_dir / filename
        n = 1
        while True:
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
            n += 1
            candidate = dest_dir / (f"{stem} ({n}){dot}{ext}" if dot else f"{stem} ({n})")

    @staticmethod
    def _safe_filename(name: str, ext: str, h: str) -> str:
        name = name.replace("/", "-").replace("\\", "-").replace("\x00", "").strip()
        name = name.lstrip(".")               # never a hidden/".." file
        if not name:
            name = f"{h[:16]}.{ext}" if ext else h[:16]
        return name

    @staticmethod
    def _assert_within(base: Path, target: Path) -> None:
        base = base.resolve()
        try:
            target.resolve().relative_to(base)
        except ValueError:
            raise VaultError(f"refusing to write outside the vault: {target} !< {base}")
