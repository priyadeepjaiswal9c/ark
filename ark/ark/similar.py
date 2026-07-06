"""Similarity intelligence — near-duplicate clusters and blurry-photo detection.

Read-mostly analysis built on the perceptual signals (``phash``/``blur``) that
extraction records for every image. It answers two questions a real camera roll
always raises:

  * "Which of these are the *same shot* I took five times?" — near-duplicate
    groups (visually similar, but NOT byte-identical; the exact byte copies are
    already handled by content-hash dedup and the cleanup report).
  * "Which photos are out of focus?" — blurry candidates.

Both are advisory. This module only *reads* and can *backfill* missing signals
(computing them from the vault object, never the source); it never moves or
deletes anything. Acting on its findings is the job of the reversible
``ark quarantine`` flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import perceptual
from .db import Database


@dataclass
class SimilarImage:
    id: int
    source_path: str
    organized_path: Optional[str]
    hash: str
    size: int
    blur: Optional[float]
    phash: Optional[str]

    @property
    def display(self) -> str:
        return self.organized_path or Path(self.source_path).name


@dataclass
class NearDupGroup:
    members: list[SimilarImage]      # >= 2, each a distinct content hash
    keep_id: int                     # the suggested keeper (sharpest, then largest)

    @property
    def keeper(self) -> SimilarImage:
        return next(m for m in self.members if m.id == self.keep_id)

    @property
    def redundant(self) -> list[SimilarImage]:
        return [m for m in self.members if m.id != self.keep_id]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(m.size for m in self.redundant)


@dataclass
class SimilarReport:
    near_dup_groups: list[NearDupGroup] = field(default_factory=list)
    blurry: list[SimilarImage] = field(default_factory=list)
    images_scanned: int = 0
    distance: int = perceptual.DEFAULT_NEAR_DUP_DISTANCE
    blur_threshold: float = perceptual.DEFAULT_BLUR_THRESHOLD

    @property
    def near_dup_reclaimable_bytes(self) -> int:
        return sum(g.reclaimable_bytes for g in self.near_dup_groups)

    def summary(self) -> str:
        return (
            f"{len(self.near_dup_groups)} near-duplicate group(s) "
            f"({sum(len(g.redundant) for g in self.near_dup_groups)} redundant, "
            f"{_human(self.near_dup_reclaimable_bytes)} reclaimable); "
            f"{len(self.blurry)} blurry; {self.images_scanned} images analyzed."
        )


def analyze_vault(
    db: Database,
    vault: str | Path,
    distance: int = perceptual.DEFAULT_NEAR_DUP_DISTANCE,
    blur_threshold: float = perceptual.DEFAULT_BLUR_THRESHOLD,
    backfill: bool = True,
) -> SimilarReport:
    """Group near-duplicate images and flag blurry ones.

    One representative per distinct content hash is used, so exact byte copies
    never masquerade as *near* duplicates. When ``backfill`` is set, images that
    lack a stored perceptual signal (e.g. ingested before P2) are computed on
    the fly from their vault object and persisted.
    """
    vault = Path(vault)
    rows = db.images_for_similarity()

    # One representative per content hash (lowest id — rows are id-ordered).
    reps: dict[str, SimilarImage] = {}
    for r in rows:
        if r["hash"] in reps:
            continue
        reps[r["hash"]] = SimilarImage(
            id=r["id"], source_path=r["source_path"], organized_path=r["organized_path"],
            hash=r["hash"], size=r["size"], blur=r["blur"], phash=r["phash"],
        )

    if backfill:
        _backfill(db, vault, reps.values())

    rep_list = list(reps.values())
    report = SimilarReport(
        images_scanned=len(rep_list), distance=distance, blur_threshold=blur_threshold)

    # near-duplicate clusters over the perceptual hashes
    items = [(im.id, im.phash) for im in rep_list if im.phash]
    by_id = {im.id: im for im in rep_list}
    for cluster in perceptual.cluster_near_duplicates(items, distance):
        members = [by_id[i] for i in cluster]
        keep = _pick_keeper(members)
        report.near_dup_groups.append(NearDupGroup(members=members, keep_id=keep.id))
    report.near_dup_groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)

    # blurry candidates
    report.blurry = sorted(
        (im for im in rep_list if im.blur is not None and im.blur < blur_threshold),
        key=lambda im: im.blur,
    )
    return report


_BLUR_TIE = 0.10   # blurs within 10% of each other count as "equally sharp"


def _pick_keeper(members: list[SimilarImage]) -> SimilarImage:
    """Choose which copy of a near-duplicate to preserve.

    Prefer the sharpest — that's the point of near-duplicate dedup for burst
    shots. But when two copies are essentially *equally* sharp (within 10%),
    the difference is just re-compression noise, so keep the higher-fidelity
    (larger) file; earliest-seen breaks a final tie. This keeps the original
    over a downscaled/re-saved edit, and the in-focus frame over a soft one."""
    best = members[0]
    for m in members[1:]:
        if _is_better_keep(m, best):
            best = m
    return best


def _is_better_keep(a: SimilarImage, b: SimilarImage) -> bool:
    ba = a.blur if a.blur is not None else -1.0
    bb = b.blur if b.blur is not None else -1.0
    hi = max(ba, bb)
    if hi > 0 and abs(ba - bb) <= _BLUR_TIE * hi:      # near-tie on sharpness
        if a.size != b.size:
            return a.size > b.size
        return a.id < b.id
    return ba > bb


def _backfill(db: Database, vault: Path, images) -> None:
    changed = False
    for im in images:
        if im.phash is not None:
            continue
        obj = db.get_object(im.hash)
        target = (vault / obj["object_path"]) if obj else Path(im.source_path)
        if not target.exists():
            continue
        phash, blur = perceptual.analyze(target)
        if phash is None and blur is None:
            continue
        im.phash, im.blur = phash, blur
        db.set_perceptual(im.id, phash, blur)
        changed = True
    if changed:
        db.commit()


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"
