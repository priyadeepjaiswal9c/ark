"""The metadata store — SQLite, with an FTS5 index for natural-language search.

Schema:
  objects       content-addressed dedup store (one row per distinct hash)
  assets        one row per ingested source file (dups included, status marks them)
  tags          asset -> tag (many)
  custom_fields asset -> (key, value)  — the parametric extension
  versions      source file identity -> the hashes seen there over time
  runs          audit log of every scan (mode, source, counts)
  assets_fts    FTS5 index over searchable text (falls back to LIKE if unavailable)

Postgres + pgvector is the documented scale path; the access is funnelled through
this class so that swap stays local.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Asset, GeoPlace

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.fts = _fts5_available(self.conn)
        self._migrate()

    # ---- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

            CREATE TABLE IF NOT EXISTS objects (
                hash TEXT PRIMARY KEY,
                object_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                ext TEXT, kind TEXT, mime TEXT,
                refcount INTEGER NOT NULL DEFAULT 0,
                first_stored_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
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
                phash TEXT, blur REAL,
                first_seen TEXT, last_seen TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_assets_hash ON assets(hash);
            CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
            CREATE INDEX IF NOT EXISTS idx_assets_taken ON assets(taken_at);

            CREATE TABLE IF NOT EXISTS tags (
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                UNIQUE(asset_id, tag)
            );

            CREATE TABLE IF NOT EXISTS custom_fields (
                asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                key TEXT NOT NULL, value TEXT,
                UNIQUE(asset_id, key)
            );

            CREATE TABLE IF NOT EXISTS versions (
                logical_path TEXT NOT NULL,
                hash TEXT NOT NULL,
                asset_id INTEGER,
                version_no INTEGER NOT NULL,
                ts TEXT NOT NULL,
                UNIQUE(logical_path, hash)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, finished_at TEXT,
                source TEXT, mode TEXT, counts TEXT, notes TEXT
            );

            -- Reversible quarantine: one row per organized/ entry relocated into
            -- quarantine/. The vault OBJECT and the SOURCE are never touched, so
            -- every row is fully undoable. Active rows have restored_at IS NULL.
            CREATE TABLE IF NOT EXISTS quarantine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch TEXT NOT NULL,
                asset_id INTEGER,
                source_path TEXT NOT NULL,
                hash TEXT NOT NULL,
                reason TEXT,
                original_relpath TEXT NOT NULL,
                quarantine_relpath TEXT NOT NULL,
                quarantined_at TEXT NOT NULL,
                restored_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_quarantine_batch ON quarantine(batch);
            CREATE INDEX IF NOT EXISTS idx_quarantine_active
                ON quarantine(source_path) WHERE restored_at IS NULL;
            """
        )
        self._ensure_columns()
        if self.fts:
            c.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
                    text, tokenize='porter unicode61'
                );
                """
            )
        c.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        c.commit()

    def _ensure_columns(self) -> None:
        """Additive migration for vaults created before a column existed.

        ``CREATE TABLE IF NOT EXISTS`` never adds columns to an existing table,
        so a v1 vault keeps its old ``assets`` shape. Add anything missing with
        ALTER TABLE (nullable, no default) — existing rows read the new columns
        as NULL and a rescan backfills them."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(assets)")}
        for col, decl in (("phash", "TEXT"), ("blur", "REAL")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE assets ADD COLUMN {col} {decl}")
        # Index built here (not in the schema script) so it works whether phash
        # was created inline (fresh vault) or just added by the ALTER above.
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_phash ON assets(phash)")

    # ---- objects (dedup store) -------------------------------------------
    def get_object(self, h: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM objects WHERE hash=?", (h,)).fetchone()

    def register_object(self, h: str, object_path: str, size: int, ext: str,
                        kind: str, mime: str, now: str) -> None:
        self.conn.execute(
            """INSERT INTO objects(hash,object_path,size,ext,kind,mime,refcount,first_stored_at)
               VALUES(?,?,?,?,?,?,0,?)
               ON CONFLICT(hash) DO NOTHING""",
            (h, object_path, size, ext, kind, mime, now),
        )

    def bump_refcount(self, h: str, delta: int = 1) -> None:
        # Floor at 0 — a decrement must never drive the count negative.
        self.conn.execute(
            "UPDATE objects SET refcount=MAX(0, refcount+?) WHERE hash=?", (delta, h))

    # ---- assets -----------------------------------------------------------
    def get_asset_by_source(self, source_path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM assets WHERE source_path=?", (source_path,)
        ).fetchone()

    def upsert_asset(self, a: Asset, now: str) -> int:
        pc = a.place
        row = (
            a.source_path, a.hash, a.size, a.ext, a.kind, a.mime,
            _iso(a.fs_created), _iso(a.fs_modified),
            _iso(a.taken_at), a.taken_at_source,
            a.lat, a.lon,
            pc.city if pc else None, pc.admin if pc else None,
            pc.country if pc else None, pc.country_code if pc else None,
            pc.distance_km if pc else None,
            a.camera_make, a.camera_model, a.width, a.height,
            a.organized_path, a.matched_rule, a.status,
            json.dumps(a.needs_review_reasons) if a.needs_review_reasons else None,
            a.phash, a.blur,
        )
        cur = self.conn.execute(
            """INSERT INTO assets(
                    source_path,hash,size,ext,kind,mime,fs_created,fs_modified,
                    taken_at,taken_at_source,lat,lon,place_city,place_admin,
                    place_country,place_cc,place_dist_km,camera_make,camera_model,
                    width,height,organized_path,matched_rule,status,needs_review,
                    phash,blur,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_path) DO UPDATE SET
                    hash=excluded.hash, size=excluded.size, kind=excluded.kind,
                    taken_at=excluded.taken_at, taken_at_source=excluded.taken_at_source,
                    lat=excluded.lat, lon=excluded.lon, place_city=excluded.place_city,
                    place_admin=excluded.place_admin, place_country=excluded.place_country,
                    place_cc=excluded.place_cc, place_dist_km=excluded.place_dist_km,
                    camera_make=excluded.camera_make, camera_model=excluded.camera_model,
                    width=excluded.width, height=excluded.height,
                    organized_path=excluded.organized_path, matched_rule=excluded.matched_rule,
                    status=excluded.status, needs_review=excluded.needs_review,
                    phash=excluded.phash, blur=excluded.blur,
                    last_seen=excluded.last_seen""",
            row + (now, now),
        )
        aid = cur.lastrowid
        existing = self.get_asset_by_source(a.source_path)
        aid = existing["id"] if existing else aid
        a.id = aid

        self.conn.execute("DELETE FROM tags WHERE asset_id=?", (aid,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO tags(asset_id,tag) VALUES(?,?)",
            [(aid, t) for t in a.tags],
        )
        self.conn.execute("DELETE FROM custom_fields WHERE asset_id=?", (aid,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO custom_fields(asset_id,key,value) VALUES(?,?,?)",
            [(aid, k, json.dumps(v) if not isinstance(v, str) else v) for k, v in a.custom.items()],
        )
        self._reindex_fts(aid, a)
        return aid

    def record_version(self, logical_path: str, h: str, asset_id: int, now: str) -> None:
        n = self.conn.execute(
            "SELECT COUNT(*) FROM versions WHERE logical_path=?", (logical_path,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT OR IGNORE INTO versions(logical_path,hash,asset_id,version_no,ts) "
            "VALUES(?,?,?,?,?)",
            (logical_path, h, asset_id, n + 1, now),
        )

    # ---- FTS --------------------------------------------------------------
    def _reindex_fts(self, aid: int, a: Asset) -> None:
        if not self.fts:
            return
        text = _searchable_text(a)
        self.conn.execute("DELETE FROM assets_fts WHERE rowid=?", (aid,))
        self.conn.execute("INSERT INTO assets_fts(rowid,text) VALUES(?,?)", (aid, text))

    # ---- runs -------------------------------------------------------------
    def start_run(self, source: str, mode: str, now: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at,source,mode) VALUES(?,?,?)", (now, source, mode)
        )
        return cur.lastrowid

    def finish_run(self, run_id: int, counts: dict, now: str, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, counts=?, notes=? WHERE id=?",
            (now, json.dumps(counts), notes, run_id),
        )

    # ---- queries ----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        c = self.conn
        assets = c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        objects = c.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        return {
            "assets": assets,
            "objects": objects,
            "bytes_stored": c.execute("SELECT COALESCE(SUM(size),0) FROM objects").fetchone()[0],
            "bytes_ingested": c.execute("SELECT COALESCE(SUM(size),0) FROM assets").fetchone()[0],
            # redundant source copies = sources minus distinct content (stable across rescans)
            "duplicates": max(0, assets - objects),
            "needs_review": c.execute(
                "SELECT COUNT(*) FROM assets WHERE status='needs_review'").fetchone()[0],
            "by_kind": {
                r[0]: r[1] for r in c.execute(
                    "SELECT kind,COUNT(*) FROM assets GROUP BY kind ORDER BY 2 DESC")
            },
        }

    def commit(self) -> None:
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def all_assets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM assets ORDER BY id").fetchall()

    def images_for_similarity(self) -> list[sqlite3.Row]:
        """Image assets that carry (or should carry) a perceptual signal.

        Ordered by id so a stable representative is picked per content-hash.
        Excludes failed assets — there's nothing to compare or quarantine."""
        return self.conn.execute(
            """SELECT id, source_path, hash, organized_path, matched_rule,
                      size, taken_at, place_city, place_country, status,
                      phash, blur
               FROM assets
               WHERE kind='image' AND status != 'failed'
               ORDER BY id"""
        ).fetchall()

    def set_perceptual(self, asset_id: int, phash: Optional[str], blur: Optional[float]) -> None:
        """Backfill a computed perceptual signal onto an existing asset row."""
        self.conn.execute(
            "UPDATE assets SET phash=?, blur=? WHERE id=?", (phash, blur, asset_id))

    def objects_for_verify(self) -> Iterable[sqlite3.Row]:
        return self.conn.execute("SELECT hash,object_path,size FROM objects").fetchall()

    # ---- quarantine (reversible declutter) --------------------------------
    def add_quarantine_entry(self, batch: str, asset_id: Optional[int], source_path: str,
                             h: str, reason: str, original_relpath: str,
                             quarantine_relpath: str, now: str) -> None:
        self.conn.execute(
            """INSERT INTO quarantine(batch,asset_id,source_path,hash,reason,
                    original_relpath,quarantine_relpath,quarantined_at,restored_at)
               VALUES(?,?,?,?,?,?,?,?,NULL)""",
            (batch, asset_id, source_path, h, reason, original_relpath, quarantine_relpath, now),
        )

    def active_quarantine_sources(self) -> set[str]:
        """Source paths whose organized/ entry is currently in quarantine — the
        pipeline must not re-materialize their organized link on rescan."""
        return {r[0] for r in self.conn.execute(
            "SELECT source_path FROM quarantine WHERE restored_at IS NULL")}

    def quarantine_batches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT batch, reason, COUNT(*) AS n, MIN(quarantined_at) AS at,
                      SUM(CASE WHEN restored_at IS NULL THEN 1 ELSE 0 END) AS active
               FROM quarantine
               GROUP BY batch
               HAVING active > 0
               ORDER BY at DESC"""
        ).fetchall()

    def quarantine_entries(self, batch: str, active_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM quarantine WHERE batch=?"
        if active_only:
            sql += " AND restored_at IS NULL"
        return self.conn.execute(sql + " ORDER BY id", (batch,)).fetchall()

    def mark_quarantine_restored(self, entry_id: int, now: str) -> None:
        self.conn.execute(
            "UPDATE quarantine SET restored_at=? WHERE id=?", (now, entry_id))


# ---- module helpers --------------------------------------------------------

def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _searchable_text(a: Asset) -> str:
    parts: list[str] = [
        Path(a.source_path).name, a.kind or "", a.ext or "",
        a.camera_make or "", a.camera_model or "",
        a.organized_path or "", a.matched_rule or "",
    ]
    if a.place:
        parts += [a.place.city or "", a.place.admin or "", a.place.country or ""]
    if a.taken_at:
        parts += [a.taken_at.strftime("%Y %B %B-%Y %A"), str(a.taken_at.year)]
    parts += list(a.tags)
    parts += [f"{k} {v}" for k, v in a.custom.items()]
    return " ".join(p for p in parts if p)
