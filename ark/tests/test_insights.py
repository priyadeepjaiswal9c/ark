"""The reasoning layer (P5): keep-score, missing-backup (single-copy), devices."""

from pathlib import Path

from ark import insights as I
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database


def _row(**kw):
    base = dict(id=1, source_path="/x/a.jpg", organized_path=None, kind="image",
                ext="jpg", lat=None, lon=None, taken_at=None, taken_at_source=None,
                camera_make=None, camera_model=None, blur=None, width=4000, height=3000,
                status="stored", matched_rule=None)
    base.update(kw)
    return base


def test_geotagged_camera_photo_is_precious():
    r = _row(lat=15.5, lon=73.8, taken_at_source="exif", camera_make="samsung",
             matched_rule="goa-winter-trips", blur=300.0)
    ks = I.keep_score(r, set(), 60.0)
    assert ks.bucket == I.PRECIOUS and ks.score >= I._PRECIOUS_AT
    assert any("geotagged" in why for why in ks.reasons)


def test_blurry_screenshot_is_junk():
    r = _row(source_path="/x/screenshot_home.png", ext="png", width=300, height=200, blur=2.0)
    ks = I.keep_score(r, set(), 60.0)
    assert ks.bucket == I.JUNK and ks.score <= I._JUNK_AT
    joined = " ".join(ks.reasons)
    assert "blurry" in joined and "screenshot" in joined


def test_near_duplicate_is_penalised():
    kw = dict(lat=1.0, lon=2.0, taken_at_source="exif", camera_make="Canon", blur=200.0)
    base = I.keep_score(_row(id=1, **kw), set(), 60.0)
    dup = I.keep_score(_row(id=2, **kw), {2}, 60.0)
    assert dup.score == base.score - 12
    assert any("near-duplicate" in why for why in dup.reasons)


def test_analyze_buckets_and_junk(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = I.analyze_vault(db, vault)
    assert sum(rep.buckets.values()) == rep.total == 14
    junk_names = {Path(k.source_path).name for k in rep.junk}
    assert "screenshot_no_exif.png" in junk_names             # the obvious junk


def test_missing_backup_flags_only_single_copies(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = I.analyze_vault(db, vault)
    single = {Path(k.source_path).name for k in rep.precious_single_copy}
    # the geotagged trip photos are precious AND unique -> flagged to mirror
    assert "IMG_20251226_173000.jpg" in single
    # the exact-duplicate pair (2 sources, 1 content) is NOT a single copy
    assert "copy_of_goa.jpg" not in single
    assert "IMG_20251224_101500.jpg" not in single


def test_device_coverage_and_gap(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = I.analyze_vault(db, vault)
    devices = {d.device: d for d in rep.devices}
    assert any("samsung" in d for d in devices)
    sam = next(d for name, d in devices.items() if "samsung" in name)
    # the sample has an Aug->Nov jump on the Samsung -> a multi-week gap is found
    assert sam.largest_gap_days >= I._GAP_ALERT_DAYS
    assert sam.first and sam.last and sam.first <= sam.last


def test_insights_backfills_similarity_in_memory_without_persisting(scanned):
    _, vault, _ = scanned
    db_path = vault / DIR_META / DB_FILENAME
    with Database(db_path) as db:
        db.conn.execute("UPDATE assets SET phash=NULL, blur=NULL WHERE kind='image'")
        db.commit()

    with Database(db_path, read_only=True) as db:
        rep = I.analyze_vault(db, vault)
    edited = next(
        k for k in rep.precious_single_copy
        if Path(k.source_path).name == "IMG_20250704_183000_edited.jpg"
    )
    assert any("near-duplicate" in reason for reason in edited.reasons)

    with Database(db_path, read_only=True) as db:
        persisted = db.conn.execute(
            "SELECT COUNT(*) FROM assets WHERE phash IS NOT NULL OR blur IS NOT NULL"
        ).fetchone()[0]
    assert persisted == 0
