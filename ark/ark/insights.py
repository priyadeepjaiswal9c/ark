"""The reasoning layer (P5) — judgments over the vault, all advisory.

Everything ARK already knows about an asset (place, capture time, camera,
sharpness, duplication) is turned into three kinds of reasoning, none of which
ever moves or deletes anything:

  * **precious-vs-junk** — a transparent *keep score* per asset. Every point is
    justified by a named reason, so the verdict is explainable, not a black box.
  * **missing-backup** — precious memories that exist as a *single copy* (no
    duplicate anywhere), i.e. single points of failure worth mirroring off-site.
  * **cross-device coverage** — per-camera/-device counts, date span, and the
    largest gap between shots (a proxy for "you have photos you never backed
    up from that stretch").

This is the seam where a heavier ML layer (faces, aesthetics, CLIP tags) could
later refine the score — but the rule-based reasoning here needs no model and
runs offline.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import perceptual, similar as similar_mod
from .constants import KIND_IMAGE
from .db import Database

PRECIOUS = "precious"
NORMAL = "normal"
JUNK = "junk"

_PRECIOUS_AT = 68
_JUNK_AT = 32
_TINY_PIXELS = 100_000          # < ~316x316 — thumbnail/icon territory
_GAP_ALERT_DAYS = 60            # a gap this long hints at un-ingested photos


@dataclass
class KeepScore:
    asset_id: int
    source_path: str
    display: str
    kind: str
    score: int
    bucket: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class DeviceCoverage:
    device: str
    count: int
    first: Optional[str]
    last: Optional[str]
    largest_gap_days: int
    gap_between: Optional[tuple[str, str]] = None


@dataclass
class InsightsReport:
    total: int = 0
    buckets: dict = field(default_factory=lambda: {PRECIOUS: 0, NORMAL: 0, JUNK: 0})
    junk: list[KeepScore] = field(default_factory=list)
    precious_single_copy: list[KeepScore] = field(default_factory=list)
    devices: list[DeviceCoverage] = field(default_factory=list)
    blur_threshold: float = perceptual.DEFAULT_BLUR_THRESHOLD

    def summary(self) -> str:
        b = self.buckets
        gaps = sum(1 for d in self.devices if d.largest_gap_days >= _GAP_ALERT_DAYS)
        return (
            f"{self.total} assets — {b[PRECIOUS]} precious, {b[NORMAL]} normal, "
            f"{b[JUNK]} likely junk; {len(self.precious_single_copy)} precious "
            f"single-copy (mirror these); {len(self.devices)} device(s), "
            f"{gaps} with a notable capture gap."
        )


def analyze_vault(db: Database, vault: str | Path,
                  blur_threshold: float = perceptual.DEFAULT_BLUR_THRESHOLD) -> InsightsReport:
    rows = db.all_assets()
    rep = InsightsReport(total=len(rows), blur_threshold=blur_threshold)
    if not rows:
        return rep

    # redundancy: how many source files carry each content hash
    hash_counts = Counter(r["hash"] for r in rows)
    # near-duplicate redundant ids (reuse the P2 perceptual grouping)
    sim = similar_mod.analyze_vault(
        db, vault, blur_threshold=blur_threshold, backfill=True, persist=False)
    near_dup_ids = {m.id for g in sim.near_dup_groups for m in g.redundant}

    for r in rows:
        ks = keep_score(r, near_dup_ids, blur_threshold)
        rep.buckets[ks.bucket] += 1
        if ks.bucket == JUNK:
            rep.junk.append(ks)
        # precious AND the only copy of its content -> a single point of failure
        if ks.bucket == PRECIOUS and hash_counts[r["hash"]] == 1:
            rep.precious_single_copy.append(ks)

    rep.junk.sort(key=lambda k: k.score)                       # worst first
    rep.precious_single_copy.sort(key=lambda k: -k.score)      # best first
    rep.devices = _device_coverage(rows)
    return rep


def keep_score(row, near_dup_ids, blur_threshold) -> KeepScore:
    """A transparent 0–100 keep score. Starts neutral at 50; every adjustment
    records a human-readable reason."""
    score = 50
    reasons: list[str] = []

    def bump(delta: int, why: str) -> None:
        nonlocal score
        score += delta
        reasons.append(f"{'+' if delta >= 0 else ''}{delta} {why}")

    kind = row["kind"]
    name = Path(row["source_path"]).name
    has_place = row["lat"] is not None and row["lon"] is not None
    exif_dated = row["taken_at_source"] == "exif"
    has_camera = bool(row["camera_make"] or row["camera_model"])
    blur = row["blur"]
    px = (row["width"] or 0) * (row["height"] or 0)

    if has_place:
        bump(+15, "geotagged (somewhere you went)")
    if exif_dated:
        bump(+10, "real capture time (EXIF)")
    if has_camera:
        bump(+10, "taken on a camera")
    if _is_trip(row):
        bump(+10, "part of a trip/event")

    if kind == KIND_IMAGE and blur is not None and blur < blur_threshold:
        bump(-25, "blurry / out of focus")
    if row["status"] == "duplicate":
        bump(-20, "exact duplicate of another file")
    if row["id"] in near_dup_ids:
        bump(-12, "near-duplicate of a kept photo")
    if _looks_like_screenshot(row, name, has_camera, has_place, exif_dated):
        bump(-20, "looks like a screenshot / non-photo")
    if kind == KIND_IMAGE and 0 < px < _TINY_PIXELS:
        bump(-10, "very small image (thumbnail/icon)")
    if not has_place and not has_camera and row["taken_at_source"] in (None, "fs"):
        bump(-10, "no real metadata (no place, camera or capture time)")

    score = max(0, min(100, score))
    bucket = PRECIOUS if score >= _PRECIOUS_AT else JUNK if score <= _JUNK_AT else NORMAL
    return KeepScore(
        asset_id=row["id"], source_path=row["source_path"],
        display=row["organized_path"] or name, kind=kind,
        score=score, bucket=bucket, reasons=reasons,
    )


def _is_trip(row) -> bool:
    rule = (row["matched_rule"] or "").lower()
    return "trip" in rule or "goa" in rule


def _looks_like_screenshot(row, name, has_camera, has_place, exif_dated) -> bool:
    low = name.lower()
    if "screenshot" in low or "screen shot" in low or low.startswith("scr_"):
        return True
    # a PNG with no camera, no place and no real capture time is almost never a
    # photo worth the same weight as a geotagged camera shot.
    return row["ext"] == "png" and not has_camera and not has_place and not exif_dated


def _device_coverage(rows) -> list[DeviceCoverage]:
    by_device: dict[str, list[str]] = {}
    for r in rows:
        if r["kind"] not in (KIND_IMAGE, "video"):
            continue
        dev = " ".join(x for x in (r["camera_make"], r["camera_model"]) if x) or "unknown / no camera"
        by_device.setdefault(dev, [])
        if r["taken_at"]:
            by_device[dev].append(r["taken_at"])

    out: list[DeviceCoverage] = []
    for dev, dates in by_device.items():
        parsed = sorted(d for d in (_parse(x) for x in dates) if d is not None)
        gap_days, span = _largest_gap(parsed)
        out.append(DeviceCoverage(
            device=dev, count=len(dates) or _count_for(rows, dev),
            first=parsed[0].date().isoformat() if parsed else None,
            last=parsed[-1].date().isoformat() if parsed else None,
            largest_gap_days=gap_days, gap_between=span,
        ))
    out.sort(key=lambda d: -d.count)
    return out


def _count_for(rows, dev) -> int:
    n = 0
    for r in rows:
        d = " ".join(x for x in (r["camera_make"], r["camera_model"]) if x) or "unknown / no camera"
        if d == dev:
            n += 1
    return n


def _largest_gap(dts: list[datetime]) -> tuple[int, Optional[tuple[str, str]]]:
    if len(dts) < 2:
        return 0, None
    worst = 0
    span: Optional[tuple[str, str]] = None
    for a, b in zip(dts, dts[1:]):
        days = (b - a).days
        if days > worst:
            worst = days
            span = (a.date().isoformat(), b.date().isoformat())
    return worst, span


def _parse(iso: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=None)      # compare naive; we only need day deltas
    except (TypeError, ValueError):
        return None
