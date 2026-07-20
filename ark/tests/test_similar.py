"""End-to-end similarity intelligence over the sample vault: near-duplicate
grouping (distinct content, not byte copies) and blurry-photo detection."""

from pathlib import Path

from ark import similar as similar_mod
from ark.cli import main
from ark.similar import SimilarImage, _pick_keeper
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database


def _img(id, size, blur):
    return SimilarImage(id=id, source_path=f"/{id}.jpg", organized_path=None,
                        hash=f"h{id}", size=size, blur=blur, phash="ff")


def test_keeper_never_prefers_a_blurry_copy_over_unknown_blur():
    # id=1 is barely-positive blur (i.e. VERY blurry); id=2 has unknown blur but
    # is the larger, higher-fidelity file. The keeper must be the larger one,
    # not the blurry one (regression: a -1 None-sentinel would pick the blurry).
    blurry_small = _img(1, size=1000, blur=0.5)
    unknown_large = _img(2, size=5000, blur=None)
    assert _pick_keeper([blurry_small, unknown_large]).id == 2
    assert _pick_keeper([unknown_large, blurry_small]).id == 2   # order-independent


def _report(vault):
    with Database(vault / DIR_META / DB_FILENAME) as db:
        return similar_mod.analyze_vault(db, vault)


def test_near_dup_finds_the_edit_not_the_exact_copy(scanned):
    _, vault, _ = scanned
    rep = _report(vault)
    # exactly one near-duplicate group: the SF original + its re-saved edit
    assert len(rep.near_dup_groups) == 1
    g = rep.near_dup_groups[0]
    names = {Path(m.source_path).name for m in g.members}
    assert names == {"IMG_20250704_183000.jpg", "IMG_20250704_183000_edited.jpg"}
    # every member is a DISTINCT content hash — the byte-identical copy_of_goa
    # pair collapses to one representative and never shows up here.
    assert len({m.hash for m in g.members}) == len(g.members)
    all_members = {Path(m.source_path).name for grp in rep.near_dup_groups for m in grp.members}
    assert "copy_of_goa.jpg" not in all_members


def test_keeper_is_the_higher_fidelity_original(scanned):
    _, vault, _ = scanned
    g = _report(vault).near_dup_groups[0]
    # near-tied sharpness -> keep the larger (original), quarantine the small edit
    assert Path(g.keeper.source_path).name == "IMG_20250704_183000.jpg"
    assert [Path(m.source_path).name for m in g.redundant] == ["IMG_20250704_183000_edited.jpg"]
    assert g.reclaimable_bytes == g.redundant[0].size


def test_blurry_photo_is_flagged(scanned):
    _, vault, _ = scanned
    rep = _report(vault)
    blurry = {Path(b.source_path).name for b in rep.blurry}
    assert "IMG_20251103_193000.jpg" in blurry            # the out-of-focus shot
    # and it is genuinely below the sharp photos
    assert all(b.blur < rep.blur_threshold for b in rep.blurry)


def test_backfill_recomputes_missing_signals(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        # wipe the perceptual signal off every image (simulating a pre-P2 vault)
        db.conn.execute("UPDATE assets SET phash=NULL, blur=NULL WHERE kind='image'")
        db.commit()
        # with backfill off, nothing can be grouped
        bare = similar_mod.analyze_vault(db, vault, backfill=False)
        assert bare.near_dup_groups == [] and bare.blurry == []
        # with backfill on, signals are recomputed from the vault objects...
        healed = similar_mod.analyze_vault(db, vault, backfill=True)
        assert len(healed.near_dup_groups) == 1
        # ...and persisted, so a later read needs no backfill
        again = similar_mod.analyze_vault(db, vault, backfill=False)
        assert len(again.near_dup_groups) == 1


def test_distance_threshold_is_respected(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        # distance 0 == require identical dHash: the recompressed edit differs
        # by a couple of bits, so no near-dup group survives.
        strict = similar_mod.analyze_vault(db, vault, distance=0)
    assert strict.near_dup_groups == []


def test_cli_similar_persists_missing_signal_backfill(scanned):
    _, vault, _ = scanned
    db_path = vault / DIR_META / DB_FILENAME
    with Database(db_path) as db:
        db.conn.execute("UPDATE assets SET phash=NULL, blur=NULL WHERE kind='image'")
        db.commit()

    assert main(["similar", "--vault", str(vault), "--json"]) == 0

    with Database(db_path, read_only=True) as db:
        represented = db.conn.execute(
            "SELECT COUNT(DISTINCT hash) FROM assets WHERE kind='image' AND phash IS NOT NULL"
        ).fetchone()[0]
        distinct_images = db.conn.execute(
            "SELECT COUNT(DISTINCT hash) FROM assets WHERE kind='image'"
        ).fetchone()[0]
    # Exact byte duplicates share a hash and need only one persisted signal.
    assert represented == distinct_images
