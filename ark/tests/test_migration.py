"""Schema v1 -> v2 upgrade: adding perceptual columns to an existing vault must
be additive and lossless (no rebuild, no data touched)."""

import sqlite3

from ark.db import Database


def _make_v1_db(path):
    """A minimal pre-P2 (v1) vault DB: an `assets` table with no phash/blur,
    one row of real data, schema_version=1, and no quarantine table."""
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            hash TEXT NOT NULL,
            size INTEGER, ext TEXT, kind TEXT, mime TEXT,
            fs_created TEXT, fs_modified TEXT,
            taken_at TEXT, taken_at_source TEXT,
            lat REAL, lon REAL,
            place_city TEXT, place_admin TEXT, place_country TEXT,
            place_cc TEXT, place_dist_km REAL,
            camera_make TEXT, camera_model TEXT,
            width INTEGER, height INTEGER,
            organized_path TEXT, matched_rule TEXT,
            status TEXT, needs_review TEXT,
            first_seen TEXT, last_seen TEXT
        );
        CREATE TABLE objects (
            hash TEXT PRIMARY KEY, object_path TEXT NOT NULL, size INTEGER NOT NULL,
            ext TEXT, kind TEXT, mime TEXT, refcount INTEGER NOT NULL DEFAULT 0,
            first_stored_at TEXT NOT NULL
        );
        INSERT INTO meta(key,value) VALUES('schema_version','1');
        INSERT INTO assets(source_path,hash,size,kind,status)
            VALUES('/phone/DCIM/old.jpg','abc123',1000,'image','stored');
        """
    )
    c.commit()
    c.close()


def test_v1_to_v2_is_additive_and_lossless(tmp_path):
    db_path = tmp_path / "ark.db"
    _make_v1_db(db_path)

    db = Database(db_path)                     # opening runs the migration
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(assets)")}
    assert "phash" in cols and "blur" in cols  # columns added
    # the pre-existing row is untouched, new columns read as NULL
    row = db.conn.execute("SELECT * FROM assets WHERE source_path='/phone/DCIM/old.jpg'").fetchone()
    assert row["hash"] == "abc123" and row["phash"] is None and row["blur"] is None
    # the quarantine machinery is now present and schema_version bumped
    assert db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='quarantine'").fetchone()
    assert db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    db.close()

    # re-opening is idempotent (no duplicate-column error, columns still there)
    db2 = Database(db_path)
    cols2 = {r["name"] for r in db2.conn.execute("PRAGMA table_info(assets)")}
    assert "phash" in cols2 and "blur" in cols2
    db2.close()
