"""Search — natural-language-ish queries over the vault.

A query is split into structured filters (``kind:image``, ``place:Goa``,
``year:2025``, ``type:pdf``, ``camera:pixel``, ``status:duplicate``) and free
text. Free text hits the FTS5 index (ranked by bm25) or a LIKE fallback; filters
become hard WHERE constraints. So "the PDF from the Goa trip" becomes free terms
[pdf, goa, trip] matched against filename/place/tags — exactly the demo query.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from .db import Database

_STOP = {"the", "a", "an", "of", "from", "in", "on", "at", "to", "and", "my", "with", "for"}
_FILTER_KEYS = {
    "kind": "kind", "type": "kind", "ext": "ext", "place": "__place__",
    "city": "place_city", "country": "place_country", "camera": "camera_model",
    "status": "status", "rule": "matched_rule",
}
# "place:" is fuzzy — a place name may be a city, region, or country.
_PLACE_COLS = ("place_city", "place_admin", "place_country")


@dataclass
class Hit:
    id: int
    source_path: str
    organized_path: Optional[str]
    kind: Optional[str]
    taken_at: Optional[str]
    place_city: Optional[str]
    place_country: Optional[str]
    status: Optional[str]
    score: float = 0.0


def search(db: Database, query: str, limit: int = 50) -> list[Hit]:
    filters, year, free = _parse(query)
    where: list[str] = []
    params: list = []

    for col, val in filters:
        if col == "__place__":
            where.append("(" + " OR ".join(f"a.{c} LIKE ?" for c in _PLACE_COLS) + ")")
            params += [f"%{val}%"] * len(_PLACE_COLS)
        else:
            where.append(f"a.{col} LIKE ?")
            params.append(f"%{val}%")
    if year:
        where.append("a.taken_at LIKE ?")
        params.append(f"{year}%")

    if free and db.fts:
        match = " OR ".join(_fts_term(t) for t in free)
        sql = (
            "SELECT a.*, bm25(assets_fts) AS score FROM assets_fts "
            "JOIN assets a ON a.id = assets_fts.rowid "
            "WHERE assets_fts MATCH ?"
        )
        params_full = [match] + params
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY score LIMIT ?"
        params_full.append(limit)
        rows = db.conn.execute(sql, params_full).fetchall()
    else:
        # LIKE fallback (no FTS) or pure-filter query.
        for t in free:
            where.append(
                "(a.source_path LIKE ? OR a.place_city LIKE ? OR a.place_country LIKE ? "
                "OR a.camera_model LIKE ? OR a.organized_path LIKE ? OR a.kind LIKE ? "
                "OR a.id IN (SELECT asset_id FROM tags WHERE tag LIKE ?))"
            )
            params += [f"%{t}%"] * 7
        sql = "SELECT a.*, 0 AS score FROM assets a"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.taken_at DESC LIMIT ?"
        params.append(limit)
        rows = db.conn.execute(sql, params).fetchall()

    return [_to_hit(r) for r in rows]


def _parse(query: str) -> tuple[list[tuple[str, str]], Optional[str], list[str]]:
    filters: list[tuple[str, str]] = []
    year: Optional[str] = None
    free: list[str] = []
    for tok in query.split():
        if ":" in tok:
            key, _, val = tok.partition(":")
            key = key.lower()
            if key == "year" and val.isdigit():
                year = val
                continue
            if key in _FILTER_KEYS and val:
                filters.append((_FILTER_KEYS[key], val))
                continue
        m = re.fullmatch(r"(19|20)\d{2}", tok)
        if m:
            year = tok
            continue
        term = tok.lower().strip(".,!?\"'")
        if term and term not in _STOP:
            free.append(term)
    return filters, year, free


def _fts_term(term: str) -> str:
    safe = re.sub(r'[^0-9a-zA-Z]', "", term)
    return f'"{safe}"*' if safe else '""'


def _to_hit(r: sqlite3.Row) -> Hit:
    return Hit(
        id=r["id"], source_path=r["source_path"], organized_path=r["organized_path"],
        kind=r["kind"], taken_at=r["taken_at"], place_city=r["place_city"],
        place_country=r["place_country"], status=r["status"],
        score=r["score"] if "score" in r.keys() else 0.0,
    )
