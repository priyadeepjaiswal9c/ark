"""The pipeline: ingest → extract → enrich → dedup → organize → back up → index.

One pass over a source dump. Non-destructive throughout — the only writes are
into the vault (via VaultWriter) and the metadata DB. ``dry_run=True`` performs
zero vault/DB writes and returns an accurate preview (including within-run dedup).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import extract as extract_mod
from . import enrich as enrich_mod
from .config import Config
from .constants import (
    DIR_META, DIR_OBJECTS, DIR_ORGANIZED, DIR_QUARANTINE, IGNORE_NAMES,
    IGNORE_PREFIXES, STATUS_DUPLICATE, STATUS_FAILED, STATUS_NEEDS_REVIEW,
    STATUS_STORED,
)
from .db import Database
from .backup import VaultWriter
from .models import Asset
from .rules import apply_rules


@dataclass
class ItemResult:
    source_path: str
    hash: str
    status: str
    organized_path: str
    matched_rule: Optional[str]
    is_new_object: bool
    size: int
    kind: str = ""
    unchanged: bool = False     # rescan of an already-stored, unchanged file
    healed: bool = False        # a corrupt vault object was rewritten from this source
    error: str = ""


@dataclass
class RunStats:
    source: str
    mode: str
    total: int = 0
    stored: int = 0
    duplicates: int = 0
    needs_review: int = 0
    unchanged: int = 0
    healed: int = 0
    failed: int = 0
    bytes_new: int = 0
    bytes_dup: int = 0
    by_kind: dict = field(default_factory=dict)
    items: list = field(default_factory=list)

    def as_counts(self) -> dict:
        return {
            "total": self.total, "stored": self.stored, "duplicates": self.duplicates,
            "needs_review": self.needs_review, "unchanged": self.unchanged,
            "healed": self.healed, "failed": self.failed,
            "bytes_new": self.bytes_new, "bytes_dup": self.bytes_dup,
            "by_kind": self.by_kind,
        }


def ingest(source: str | Path) -> Iterator[Path]:
    """Yield every real file under ``source`` (recursive), skipping OS cruft
    and the vault's own directories if the source overlaps the vault."""
    root = Path(source)
    if root.is_file():
        yield root
        return
    skip_dirs = {DIR_META, DIR_OBJECTS, DIR_ORGANIZED, DIR_QUARANTINE}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".ark")]
        for name in filenames:
            if name in IGNORE_NAMES or name.startswith(IGNORE_PREFIXES):
                continue
            p = Path(dirpath) / name
            if p.is_file() and not p.is_symlink():
                yield p


def run(
    source: str | Path,
    vault: str | Path,
    cfg: Config,
    dry_run: bool = False,
    progress: Optional[Callable[[ItemResult], None]] = None,
) -> RunStats:
    vault = Path(vault).resolve()
    writer = VaultWriter(vault, cfg)
    stats = RunStats(source=str(Path(source).resolve()), mode="dry-run" if dry_run else "commit")

    db = Database(vault / DIR_META / "ark.db") if not dry_run else Database(":memory:")
    if dry_run:
        _seed_readonly(db, vault)

    now = _now()
    run_id = db.start_run(stats.source, stats.mode, now) if not dry_run else -1
    seen: set[str] = set()
    # Sources whose organized/ link is in quarantine — never re-materialize it.
    quarantined = db.active_quarantine_sources()

    try:
        for i, path in enumerate(ingest(source), 1):
            item = _process_one(path, cfg, db, writer, seen, dry_run, quarantined)
            _tally(stats, item)
            if progress:
                progress(item)
            if not dry_run and i % 50 == 0:
                db.commit()
        if not dry_run:
            db.finish_run(run_id, stats.as_counts(), _now())
            db.set_meta("last_scan", _now())
            db.commit()
    finally:
        db.close()
    return stats


def _process_one(path: Path, cfg: Config, db: Database, writer: VaultWriter,
                 seen: set[str], dry_run: bool, quarantined: set[str]) -> ItemResult:
    # One bad file — corrupt bytes, a rule that raises, a transient DB error —
    # must NEVER abort the whole scan. The entire per-file body is guarded.
    try:
        asset = extract_mod.extract(path)
        enrich_mod.enrich(asset)
        match = apply_rules(asset, cfg)
        asset.matched_rule = match.rule_name
        for t in match.tags:
            if t not in asset.tags:
                asset.tags.append(t)
        asset.needs_review_reasons = [f"missing:{f}" for f in match.unknown_fields]

        existing = db.get_asset_by_source(asset.source_path)
        is_rescan = existing is not None and existing["hash"] == asset.hash
        in_run_dup = asset.hash in seen  # identical content already stored earlier this run
        # If this source's organized entry is currently quarantined, keep it that
        # way: back the object up but don't recreate the organized/ link.
        is_quarantined = asset.source_path in quarantined

        result = writer.store(asset, match, dry_run=dry_run, skip_organize=is_quarantined)

        unchanged = healed = False
        wrote_bytes = result.action in ("stored", "healed")  # store actually copied bytes
        if result.action == "failed":
            asset.status = STATUS_FAILED
        elif wrote_bytes:
            # Bytes were written this run. If this was a rescan of an unchanged
            # source, it means the vault had LOST or corrupted the object and we
            # just rebuilt it — surface that as a heal, never as "unchanged".
            asset.status = STATUS_NEEDS_REVIEW if match.is_review else STATUS_STORED
            healed = (result.action == "healed") or is_rescan
        elif is_rescan:
            asset.status = STATUS_STORED      # object already good, same source — no write
            unchanged = True
        elif result.action == "duplicate" or in_run_dup:
            asset.status = STATUS_DUPLICATE   # a VERIFIED good copy exists elsewhere
        elif match.is_review:
            asset.status = STATUS_NEEDS_REVIEW
        else:
            asset.status = STATUS_STORED
        if is_quarantined:
            # Physically it lives in quarantine/ now; keep the record pointing at
            # where it belongs (and where `undo` restores it), not the fallback dir.
            asset.organized_path = (existing["organized_path"] if existing else None) \
                or result.organized_relpath or match.dest_relpath
        else:
            asset.organized_path = result.organized_relpath or match.dest_relpath

        if not dry_run and result.action != "failed":
            now = _now()
            aid = db.upsert_asset(asset, now)
            db.register_object(asset.hash, result.object_relpath, asset.size,
                               asset.ext, asset.kind, asset.mime, now)
            if existing is None:
                db.bump_refcount(asset.hash, +1)
            elif existing["hash"] != asset.hash:
                db.bump_refcount(existing["hash"], -1)  # floored in db.bump_refcount
                db.bump_refcount(asset.hash, +1)
            # Version history is keyed on the SOURCE file's identity, so two
            # different files that happen to share a name never conflate.
            db.record_version(asset.source_path, asset.hash, aid, now)
        elif not dry_run:  # failed — still record the attempt so it's visible
            db.upsert_asset(asset, _now())

        if result.action != "failed":
            seen.add(asset.hash)
        return ItemResult(
            source_path=asset.source_path, hash=asset.hash, status=asset.status,
            organized_path=asset.organized_path, matched_rule=asset.matched_rule,
            is_new_object=result.is_new_object, size=asset.size, kind=asset.kind,
            unchanged=unchanged, healed=healed, error=result.error,
        )
    except Exception as e:  # noqa: BLE001 — resilience is the whole point here
        return ItemResult(str(path), "", STATUS_FAILED, "", None, False, 0,
                          error=f"{type(e).__name__}: {e}")


def _tally(stats: RunStats, item: ItemResult) -> None:
    stats.total += 1
    if item.status == STATUS_FAILED:
        stats.failed += 1
    elif item.unchanged:
        stats.unchanged += 1                 # already stored, nothing written
    elif item.status == STATUS_DUPLICATE:
        stats.duplicates += 1
        stats.bytes_dup += item.size
    elif item.status == STATUS_NEEDS_REVIEW:
        stats.needs_review += 1
        stats.bytes_new += item.size
    else:  # STATUS_STORED (new content, possibly a heal)
        stats.stored += 1
        stats.bytes_new += item.size
        if item.healed:
            stats.healed += 1
    if item.kind and item.status != STATUS_FAILED:
        stats.by_kind[item.kind] = stats.by_kind.get(item.kind, 0) + 1
    stats.items.append(item)


def _seed_readonly(mem: Database, vault: Path) -> None:
    """Seed the in-memory dry-run DB from the persistent one so the preview knows
    what's already stored (accurate dedup preview).

    Opens the real DB **read-only** (a dry-run must not mutate anything, not even
    the DB's journal mode or mtime) and tolerates a missing/corrupt file — a
    preview should degrade, never crash.
    """
    real = vault / DIR_META / "ark.db"
    if not real.exists():
        return
    try:
        ro = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        for row in ro.execute(
            "SELECT hash,object_path,size,ext,kind,mime,refcount,first_stored_at FROM objects"
        ):
            mem.conn.execute(
                "INSERT OR IGNORE INTO objects(hash,object_path,size,ext,kind,mime,refcount,first_stored_at) "
                "VALUES(?,?,?,?,?,?,?,?)", tuple(row))
        for row in ro.execute("SELECT source_path,hash FROM assets"):
            mem.conn.execute(
                "INSERT OR IGNORE INTO assets(source_path,hash) VALUES(?,?)", tuple(row))
        mem.commit()
    except sqlite3.Error:
        pass  # corrupt/locked real DB -> proceed with a best-effort (maybe empty) seed
    finally:
        ro.close()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
