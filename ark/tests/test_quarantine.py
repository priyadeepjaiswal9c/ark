"""Reversible quarantine: non-destructive move, rescan-stickiness, and exact undo.

The bar: quarantine may relocate an organized-view link, but the source and the
content-addressed object must be provably untouched, a rescan must not
un-quarantine, and undo must put everything back.
"""

from pathlib import Path

from ark import pipeline, quarantine as Q, report
from ark.backup import VaultWriter
from ark.config import load_config
from ark.constants import DIR_META, DB_FILENAME, DIR_ORGANIZED, DIR_QUARANTINE
from ark.db import Database
from ark.hashing import hash_file


def _source_digest(dump: Path) -> dict:
    return {str(p): hash_file(p) for p in sorted(dump.rglob("*")) if p.is_file()}


def _object_files(vault: Path) -> set:
    return {p.name for p in (vault / "objects").rglob("*") if p.is_file()}


def _apply(vault: Path, reason: str, dry_run=False):
    with Database(vault / DIR_META / DB_FILENAME) as db:
        plan = Q.plan(db, vault, reason)
        return Q.apply(db, vault, plan, dry_run=dry_run)


def test_quarantine_is_non_destructive(scanned):
    dump, vault, _ = scanned
    src_before = _source_digest(dump)
    obj_before = _object_files(vault)

    res = _apply(vault, "near-duplicates")
    assert len(res.moved) == 1
    moved_rel = res.moved[0].original_relpath

    # the source bytes and the object store are byte-for-byte unchanged
    assert _source_digest(dump) == src_before
    assert _object_files(vault) == obj_before
    # the organized link is gone from organized/ and now lives under quarantine/
    assert not (vault / moved_rel).exists()
    inner = moved_rel[len(DIR_ORGANIZED) + 1:]
    assert (vault / DIR_QUARANTINE / res.batch / inner).is_file()
    # and the whole vault (objects + organized + quarantine) still verifies
    problems = VaultWriter(vault, load_config(vault)).verify_vault()
    assert problems == []


def test_dry_run_moves_nothing(scanned):
    _, vault, _ = scanned
    res = _apply(vault, "near-duplicates", dry_run=True)
    assert res.moved                                  # it reports a plan
    assert not (vault / DIR_QUARANTINE).exists() or not any((vault / DIR_QUARANTINE).rglob("*"))
    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert db.active_quarantine_sources() == set()   # nothing recorded


def test_undo_restores_exactly(scanned):
    _, vault, _ = scanned
    res = _apply(vault, "near-duplicates")
    moved_rel = res.moved[0].original_relpath
    assert not (vault / moved_rel).exists()

    with Database(vault / DIR_META / DB_FILENAME) as db:
        out = Q.undo(db, vault, res.batch)
    assert out[res.batch].restored == [moved_rel]
    assert (vault / moved_rel).is_file()              # back where it belongs
    assert not any((vault / DIR_QUARANTINE).rglob("*.jpg"))  # quarantine emptied
    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert db.active_quarantine_sources() == set()
        assert db.quarantine_batches() == []


def test_rescan_does_not_unquarantine(scanned):
    dump, vault, _ = scanned
    res = _apply(vault, "near-duplicates")
    moved_rel = res.moved[0].original_relpath
    moved_src = res.moved[0].source_path

    cfg = load_config(vault)
    pipeline.run(dump, vault, cfg, dry_run=False)      # a full rescan

    # the organized link must NOT have been recreated by the rescan
    assert not (vault / moved_rel).exists()
    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert moved_src in db.active_quarantine_sources()
        # the object is still backed up + verified, so cleanup still calls the
        # SOURCE safe to delete — quarantining the *view* changes nothing there.
        rep = report.cleanup_report(db, vault)
    assert moved_src in {c.source_path for c in rep.safe_to_delete}
    assert VaultWriter(vault, cfg).verify_vault() == []


def test_undo_relocates_on_collision(scanned):
    _, vault, _ = scanned
    res = _apply(vault, "near-duplicates")
    original = vault / res.moved[0].original_relpath
    # something else re-took the original organized spot before undo
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"a different file now sits here")
    decoy = hash_file(original)

    with Database(vault / DIR_META / DB_FILENAME) as db:
        out = Q.undo(db, vault, res.batch)
    r = out[res.batch]
    assert r.restored == [] and len(r.relocated) == 1   # couldn't take the spot -> relocated
    assert original.is_file() and hash_file(original) == decoy   # decoy untouched
    wanted, actual = r.relocated[0]
    assert (vault / actual).is_file() and actual != wanted       # restored beside it


def test_duplicates_keeps_one_canonical(scanned):
    _, vault, _ = scanned
    res = _apply(vault, "duplicates")
    # exactly the redundant exact copy is quarantined; the canonical stays
    names = {Path(c.source_path).name for c in res.moved}
    assert names == {"copy_of_goa.jpg"}
    assert (vault / "organized" / "Trips" / "Goa-2025" / "IMG_20251224_101500.jpg").exists()


def test_second_quarantine_skips_already_active(scanned):
    _, vault, _ = scanned
    _apply(vault, "near-duplicates")
    with Database(vault / DIR_META / DB_FILENAME) as db:
        plan = Q.plan(db, vault, "near-duplicates")
    assert plan.candidates == []                       # nothing new to move
    assert any("already quarantined" in why for _, why in plan.skipped)
