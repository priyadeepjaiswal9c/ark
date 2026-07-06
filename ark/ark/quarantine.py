"""Reversible quarantine — declutter the organized view without ever deleting.

The sacred rule holds here too. Quarantine touches exactly one thing: the
*organized-view link* (a hardlink/copy under ``organized/``) of a redundant or
blurry asset. It ``os.rename``s that link into ``quarantine/<batch>/…`` and
records an undo manifest. It never touches:

  * the **source** file (read-only, as always), nor
  * the content-addressed **object** in ``objects/`` (still hash-verifiable,
    still referenced — refcount is unchanged).

Because the moved link is a hardlink to the very same object, *no bytes are
copied or lost* — the picture is still fully in the vault, just filed under
``quarantine/`` instead of ``organized/``. ``ark quarantine undo`` renames it
straight back. A rescan will not silently un-quarantine it: the pipeline reads
the active quarantine set and skips re-linking those sources.

Three ways to select what to quarantine:
  * ``duplicates``      — exact byte copies (keep one canonical per content).
  * ``near-duplicates`` — visually-the-same shots (keep the best per group).
  * ``blurry``          — out-of-focus photos below the blur threshold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import similar as similar_mod
from .constants import DIR_META, DIR_ORGANIZED, DIR_QUARANTINE
from .db import Database

REASONS = ("duplicates", "near-duplicates", "blurry")


@dataclass
class QCandidate:
    asset_id: int
    source_path: str
    hash: str
    size: int
    original_relpath: str        # vault-relative organized/ path being moved
    reason: str

    @property
    def display(self) -> str:
        return self.original_relpath or Path(self.source_path).name


@dataclass
class QuarantinePlan:
    reason: str
    candidates: list[QCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (display, why)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.size for c in self.candidates)


@dataclass
class ApplyResult:
    batch: Optional[str]
    moved: list[QCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.size for c in self.moved)


@dataclass
class UndoResult:
    restored: list[str] = field(default_factory=list)
    relocated: list[tuple[str, str]] = field(default_factory=list)  # (wanted, actual) on collision
    missing: list[str] = field(default_factory=list)


# ---- planning --------------------------------------------------------------

def plan(db: Database, vault: str | Path, reason: str,
         distance: Optional[int] = None, blur_threshold: Optional[float] = None) -> QuarantinePlan:
    """Decide which organized entries to quarantine for ``reason`` — no writes."""
    vault = Path(vault)
    if reason not in REASONS:
        raise ValueError(f"unknown quarantine reason: {reason!r} (pick one of {REASONS})")

    p = QuarantinePlan(reason=reason)
    active = db.active_quarantine_sources()

    if reason == "duplicates":
        raw = _duplicate_candidates(db)
    else:
        rep = similar_mod.analyze_vault(
            db, vault,
            distance=distance if distance is not None else similar_mod.perceptual.DEFAULT_NEAR_DUP_DISTANCE,
            blur_threshold=blur_threshold if blur_threshold is not None
            else similar_mod.perceptual.DEFAULT_BLUR_THRESHOLD,
        )
        if reason == "near-duplicates":
            raw = [(m.id, m.source_path, m.hash, m.size, m.organized_path)
                   for g in rep.near_dup_groups for m in g.redundant]
        else:  # blurry
            raw = [(b.id, b.source_path, b.hash, b.size, b.organized_path) for b in rep.blurry]

    for aid, src, h, size, organized in raw:
        cand = QCandidate(aid, src, h, size, organized or "", reason)
        if src in active:
            p.skipped.append((cand.display, "already quarantined"))
            continue
        if not organized:
            p.skipped.append((cand.display, "no organized entry to move"))
            continue
        if not (vault / organized).is_file():
            p.skipped.append((cand.display, "organized entry missing on disk"))
            continue
        p.candidates.append(cand)
    return p


def _duplicate_candidates(db: Database) -> list[tuple]:
    """Exact byte-duplicate asset rows to quarantine: for each content hash with
    more than one source, keep the earliest-ingested (lowest id) and offer the
    rest (their own organized links) for quarantine."""
    rows = db.conn.execute(
        """SELECT id, source_path, hash, size, organized_path
           FROM assets
           WHERE status IN ('stored','duplicate')
           ORDER BY hash, id"""
    ).fetchall()
    out: list[tuple] = []
    seen: set[str] = set()
    for r in rows:
        if r["hash"] in seen:                # a later row for this hash -> redundant
            out.append((r["id"], r["source_path"], r["hash"], r["size"], r["organized_path"]))
        else:
            seen.add(r["hash"])              # first (canonical) copy — keep it
    return out


# ---- applying --------------------------------------------------------------

def apply(db: Database, vault: str | Path, plan: QuarantinePlan, dry_run: bool = False) -> ApplyResult:
    vault = Path(vault).resolve()
    res = ApplyResult(batch=None, skipped=list(plan.skipped), dry_run=dry_run)
    if not plan.candidates:
        return res

    batch = _unique_batch(vault, plan.reason)
    res.batch = batch
    now = _now()

    for c in plan.candidates:
        inner = _strip_organized(c.original_relpath)
        q_rel = f"{DIR_QUARANTINE}/{batch}/{inner}"
        src_abs = _within(vault, c.original_relpath)
        dst_abs = _within(vault, q_rel)
        if dry_run:
            res.moved.append(c)
            continue
        try:
            dst_abs.parent.mkdir(parents=True, exist_ok=True)
            src_abs.rename(dst_abs)          # atomic move within the vault filesystem
        except OSError as e:
            res.skipped.append((c.display, f"move failed: {e}"))
            continue
        # Commit this entry NOW, before moving on. The rename is already durable,
        # so the DB row must be too — otherwise a crash mid-batch would leave the
        # file in quarantine/ with no record, and the next rescan would silently
        # re-create its organized link (un-quarantine it). One commit per entry
        # bounds any crash to a single in-flight file.
        db.add_quarantine_entry(batch, c.asset_id, c.source_path, c.hash,
                                c.reason, c.original_relpath, q_rel, now)
        db.commit()
        res.moved.append(c)

    if not dry_run and res.moved:
        _write_manifest(vault, batch, plan.reason, now, res.moved)
    return res


# ---- undo ------------------------------------------------------------------

def undo(db: Database, vault: str | Path, batch: str) -> dict[str, UndoResult]:
    """Restore one batch, or every active batch when ``batch == 'all'``."""
    vault = Path(vault).resolve()
    batches = ([r["batch"] for r in db.quarantine_batches()] if batch == "all" else [batch])
    results: dict[str, UndoResult] = {}
    for b in batches:
        results[b] = _undo_one(db, vault, b)
    return results


def _undo_one(db: Database, vault: Path, batch: str) -> UndoResult:
    res = UndoResult()
    now = _now()
    entries = db.quarantine_entries(batch, active_only=True)
    for e in entries:
        q_abs = _within(vault, e["quarantine_relpath"])
        want_abs = _within(vault, e["original_relpath"])
        if not q_abs.is_file():
            res.missing.append(e["quarantine_relpath"])
            db.mark_quarantine_restored(e["id"], now)   # nothing to move back
            db.commit()
            continue
        target = want_abs
        if target.exists() or target.is_symlink():
            target = _free_path(want_abs)               # something re-took the spot
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            q_abs.rename(target)
        except OSError:
            continue                                    # leave the row active; a retry can fix it
        # Same rule as apply(): the move is durable now, so record it now. A
        # crash before this commit leaves the row active and the file already
        # restored — a re-run's `not q_abs.is_file()` branch converges the DB.
        db.mark_quarantine_restored(e["id"], now)
        db.commit()
        rel = str(target.relative_to(vault))
        if target == want_abs:
            res.restored.append(rel)
        else:
            res.relocated.append((e["original_relpath"], rel))
    _prune_empty_dirs(vault / DIR_QUARANTINE / batch)
    _mark_manifest_undone(vault, batch, now, res)
    return res


# ---- helpers ---------------------------------------------------------------

def _strip_organized(rel: str) -> str:
    prefix = DIR_ORGANIZED + "/"
    return rel[len(prefix):] if rel.startswith(prefix) else rel


def _unique_batch(vault: Path, reason: str) -> str:
    base = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{reason}"
    batch, n = base, 1
    while (vault / DIR_QUARANTINE / batch).exists():
        n += 1
        batch = f"{base}-{n}"
    return batch


def _free_path(target: Path) -> Path:
    stem, dot, ext = target.name.partition(".")
    n = 1
    while True:
        n += 1
        cand = target.with_name(f"{stem} ({n}){dot}{ext}" if dot else f"{stem} ({n})")
        if not cand.exists() and not cand.is_symlink():
            return cand


def _within(vault: Path, rel: str) -> Path:
    """Resolve a vault-relative path and prove it stays inside the vault."""
    p = (vault / rel).resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        raise ValueError(f"refusing to touch a path outside the vault: {p}")
    return p


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _manifest_dir(vault: Path) -> Path:
    d = vault / DIR_META / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_manifest(vault: Path, batch: str, reason: str, now: str, moved: list[QCandidate]) -> None:
    payload = {
        "batch": batch, "reason": reason, "created_at": now, "vault": str(vault),
        "entries": [
            {"asset_id": c.asset_id, "source_path": c.source_path, "hash": c.hash,
             "size": c.size, "original_relpath": c.original_relpath,
             "quarantine_relpath": f"{DIR_QUARANTINE}/{batch}/{_strip_organized(c.original_relpath)}"}
            for c in moved
        ],
    }
    (_manifest_dir(vault) / f"{batch}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")


def _mark_manifest_undone(vault: Path, batch: str, now: str, res: UndoResult) -> None:
    mpath = _manifest_dir(vault) / f"{batch}.json"
    try:
        data = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"batch": batch}
    except (OSError, json.JSONDecodeError):
        data = {"batch": batch}
    data["undone_at"] = now
    data["restored"] = res.restored
    data["relocated"] = res.relocated
    (_manifest_dir(vault) / f"{batch}.undone.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    if mpath.exists():
        mpath.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
