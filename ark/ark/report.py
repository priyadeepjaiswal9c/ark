"""Reporting + the beginnings of cleanup intelligence.

`cleanup_report` answers the question ARK exists to answer: *which source files
are already safely in the vault, and therefore safe to delete from your phone?*

The bar for calling a source safe to delete is deliberately high, because the
cost of being wrong is a lost memory. A source is safe ONLY when all three hold,
re-checked against the bytes on disk *right now*:
  1. its recorded content hash has a vault object, and
  2. that object still re-hashes to its content address (backup intact), and
  3. the LIVE source file still hashes to that same value (the *current* bytes
     are what's backed up — not some older version the DB happens to remember).
Anything failing (2) or (3), or whose source can't be read to prove (3), is
surfaced as AT RISK / unverifiable — never as safe. It never deletes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .hashing import hash_file


@dataclass
class CleanupCandidate:
    source_path: str
    hash: str
    size: int
    reason: str
    canonical_object: str      # the vault object proving it's backed up
    is_exact_duplicate: bool = False


@dataclass
class AtRisk:
    source_path: str
    hash: str
    size: int
    problem: str


@dataclass
class CleanupReport:
    safe_to_delete: list[CleanupCandidate] = field(default_factory=list)
    at_risk: list[AtRisk] = field(default_factory=list)
    unverifiable: list[AtRisk] = field(default_factory=list)
    reclaimable_bytes: int = 0
    exact_duplicates: int = 0   # redundant EXTRA copies (group size - 1 per group)
    needs_review: int = 0
    total_assets: int = 0

    def summary(self) -> str:
        risk = f"; {len(self.at_risk)} AT RISK (do NOT delete)" if self.at_risk else ""
        unv = f"; {len(self.unverifiable)} unverifiable (source unreadable)" if self.unverifiable else ""
        dup = f"; {self.exact_duplicates} redundant duplicate(s)" if self.exact_duplicates else ""
        return (
            f"{len(self.safe_to_delete)} file(s) safe to clear from your phone/source "
            f"({_human(self.reclaimable_bytes)} reclaimable){dup}{risk}{unv}; "
            f"{self.needs_review} need review; {self.total_assets} assets total."
        )


def cleanup_report(db: Database, vault: str | Path) -> CleanupReport:
    vault = Path(vault)
    rep = CleanupReport()
    rep.total_assets = db.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    rep.needs_review = db.conn.execute(
        "SELECT COUNT(*) FROM assets WHERE status='needs_review'").fetchone()[0]

    # Candidates: stored/duplicate assets (NOT failed, NOT needs_review — those
    # stay in their own bucket so the categories are disjoint).
    rows = db.conn.execute(
        """SELECT a.source_path, a.hash, a.size, o.object_path
           FROM assets a JOIN objects o ON o.hash = a.hash
           WHERE a.status IN ('stored','duplicate')
           ORDER BY a.size DESC"""
    ).fetchall()

    hash_counts = Counter(r["hash"] for r in rows)   # >1 == content duplicated across sources
    dup_seen: set[str] = set()

    for r in rows:
        h, src, size, objrel = r["hash"], r["source_path"], r["size"], r["object_path"]

        # (2) is the backup object itself intact?
        # Verify for this source row. Never cache a True across duplicate rows:
        # the object may be corrupted or disappear midway through a long report.
        if not _object_verifies(vault, objrel, h):
            problem = ("vault object missing" if not (vault / objrel).exists()
                       else "vault object corrupt")
            rep.at_risk.append(AtRisk(src, h, size, problem))
            continue

        # (3) do the source's CURRENT bytes match what's backed up?
        state = _source_state(src, h)
        if state == "match":
            # Re-hash once more immediately before emitting the safety verdict.
            # This closes the long gap in which a mutable phone/source file
            # could change after its earlier proof but before classification.
            final_state = _source_state(src, h)
            if final_state != "match":
                if final_state == "changed":
                    rep.at_risk.append(AtRisk(
                        src, h, size, "source changed during cleanup verification — re-scan before deleting"))
                elif final_state == "unreadable":
                    rep.unverifiable.append(AtRisk(src, h, size, "source became unreadable during verification"))
                # Missing during proof means there is no longer anything at the
                # source path to classify as safely clearable.
                continue
            is_dup = hash_counts[h] > 1
            rep.safe_to_delete.append(CleanupCandidate(
                source_path=src, hash=h, size=size,
                reason=("exact duplicate — another copy of these bytes is in your dump"
                        if is_dup else "backed up — current source bytes verified in the vault"),
                canonical_object=objrel, is_exact_duplicate=is_dup,
            ))
            rep.reclaimable_bytes += size
            if is_dup:
                if h in dup_seen:            # count only the EXTRA copies as redundant
                    rep.exact_duplicates += 1
                else:
                    dup_seen.add(h)
        elif state == "changed":
            rep.at_risk.append(AtRisk(
                src, h, size, "source changed since last scan — re-scan before deleting"))
        elif state == "unreadable":
            rep.unverifiable.append(AtRisk(src, h, size, "source unreadable right now"))
        # state == "missing": source already gone — nothing to reclaim, skip.
    return rep


def _object_verifies(vault: Path, object_relpath: str, expected_hash: str) -> bool:
    obj = vault / object_relpath
    try:
        return obj.is_file() and hash_file(obj) == expected_hash
    except OSError:
        return False


def _source_state(src: str, expected_hash: str) -> str:
    """'match' | 'changed' | 'missing' | 'unreadable' for the live source file."""
    p = Path(src)
    try:
        if not p.is_file():
            return "missing"
        return "match" if hash_file(p) == expected_hash else "changed"
    except OSError:
        return "unreadable"


def status_report(db: Database) -> dict:
    s = db.stats()
    s["human"] = {
        "stored": _human(s["bytes_stored"]),
        "ingested": _human(s["bytes_ingested"]),
        "saved_by_dedup": _human(max(0, s["bytes_ingested"] - s["bytes_stored"])),
    }
    return s


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"
