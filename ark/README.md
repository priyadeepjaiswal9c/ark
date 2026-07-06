# ARK — the intelligent vault for your entire digital life

ARK turns an unsorted SSD dump into a **structured, enriched, versioned,
searchable vault** — and then reasons about it: what's a duplicate, what's
already safely backed up, and therefore **safe to delete from your phone**.

It is built around one real workflow (Samsung → Mac → SSD dump) and one
**sacred rule**:

> **Non-destructive by default.** ARK only ever *reads* your source files and
> *copies* them into the vault. It never moves, overwrites, or deletes an
> original. Every copy is hash-verified. Every deletion suggestion is backed by
> byte-for-byte proof. **Trust is the product.**

This repo is **P1 + P2 groundwork**: the vault core (extract → enrich →
organize → versioned safe backup), plus natural-language search, hash dedup,
and the safe-to-delete-from-phone cleanup report.

---

## Quick start

```bash
cd ark
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python Pillow piexif
PY=.venv/bin/python

# (optional) make a realistic sample dump to see it work end-to-end
$PY tools/make_sample_dump.py sample-dump

export ARK_VAULT="$PWD/demo-vault"
$PY -m ark init                      # create the vault
$PY -m ark scan sample-dump --dry-run   # preview — writes NOTHING
$PY -m ark scan sample-dump          # commit: organize + back up (sources untouched)

$PY -m ark search "the PDF from the Goa trip"
$PY -m ark search kind:image place:Goa year:2025
$PY -m ark cleanup                   # what's safe to delete from your phone
$PY -m ark similar                   # near-duplicate + blurry-photo report
$PY -m ark quarantine near-duplicates --dry-run   # preview a reversible declutter
$PY -m ark status
$PY -m ark verify                    # re-hash every object (integrity)
```

`ark` also installs as a console script (`pip install -e .` → `ark ...`).

---

## What each command does

| Command | What it does |
|---|---|
| `ark init` | Create the vault (`objects/`, `organized/`, `.ark/`) + default config. |
| `ark scan SRC [--dry-run]` | Ingest a dump: extract metadata, geocode, dedup, organize by rules, back up. `--dry-run` previews with **zero writes**. |
| `ark search QUERY` | Natural-language-ish search: free text + filters (`kind:`, `place:`, `year:`, `camera:`). |
| `ark cleanup` | The safe-to-delete-from-phone report — duplicates proven already in the vault. Suggests, never deletes. |
| `ark similar [--distance N] [--blur-threshold X]` | Find **near-duplicate** photos (visually the same shot, different bytes) and **blurry** ones. Read-only; suggests a keeper per group. |
| `ark insights` | Reasoning layer: a transparent **keep-score** (precious/normal/junk, every point justified), **missing-backup** (precious single-copy items to mirror), and **per-device** coverage + capture gaps. Read-only. |
| `ark watch [--once] [--dry-run]` | **Auto-ingest on connect** — scans a volume the moment it mounts (`/Volumes`), matched against a config allowlist. Non-destructive; a reconnected card is re-scanned (dedup makes it cheap). |
| `ark quarantine {near-duplicates,blurry,duplicates,list,undo}` | Reversibly move redundant/blurry **organized-view links** into `quarantine/` with an undo manifest. Objects and sources are never touched; `undo` restores everything. `--dry-run` previews. |
| `ark status` | Vault stats: assets, distinct objects, bytes saved by dedup, per-kind. |
| `ark rules [--validate]` | Show / validate the organization rules. |
| `ark verify` | Re-hash every stored object against its content address (corruption check). |

---

## How the vault is laid out

```
<vault>/
  objects/<ab>/<hash>.<ext>     content-addressed store — each distinct file once
  organized/<rule path>/...     human-readable tree, hardlinked into objects (no data dup)
  quarantine/<batch>/...        reversibly-moved organized links (undo restores them; never deletions)
  .ark/ark.db                   metadata DB (SQLite + FTS5)
  .ark/ark.toml                 config + rules
```

- **Dedup by design.** The hash *is* the identity, so identical bytes are stored
  exactly once. The `organized/` tree is hardlinks, so a file can appear in many
  places for free.
- **Verified writes.** New objects are written to a temp file, fsync'd, re-hashed,
  and only then atomically renamed into place. A hash mismatch fails the asset —
  the source is left untouched.
- **Versioned.** When the file at a logical path changes, the new content becomes
  a new object and a new row in `versions`; the old content is never overwritten.

## The metadata model (parametric)

Every asset is a record with: path, hash, type, size, timestamps,
**location** (GPS → offline-resolved place), **date/time** (timezone-aware),
camera, dimensions, tags — plus an open **`custom`** bag. "Add a new parameter"
= put a key in `custom`; rules can use it immediately.

## Organization rules

Rules live in `.ark/ark.toml`, are evaluated top-to-bottom (first match wins),
and are pure data:

```toml
[[rules]]
name = "goa-winter-trips"
when = 'kind == "image" and place.admin == "Goa" and taken_at.month in (11, 12, 1)'
to   = "Trips/Goa-{taken_at.year}"
tags = ["trip", "goa"]
```

The `when` expression is evaluated by a **safe AST interpreter** — never
`eval` — with a strict whitelist (no dunder access, only a fixed set of
string/list helpers). The `to` template is rendered with every substituted value
sanitized and the final path confirmed to stay inside `organized/`, so a rule can
never write outside the vault.

## Near-duplicates, blur & reversible quarantine (P2)

Every image gets two perceptual signals at scan time, computed with **Pillow
only** (no numpy/OpenCV/ML weights) and stored in the DB:

- a **dHash** fingerprint — two photos within a small Hamming distance are the
  *same shot* even if their bytes differ (re-compressed, resized, a burst
  frame). Content-hash dedup only catches byte-identical copies; this catches
  the rest.
- a **blur score** — the variance of the Laplacian; low means out-of-focus.

`ark similar` reports near-duplicate groups (suggesting which copy to keep — the
sharpest, ties broken by file size) and blurry photos. It's read-only.

`ark quarantine {near-duplicates,blurry,duplicates}` acts on those findings
**reversibly**: it `os.rename`s the redundant/blurry entry's `organized/`
hardlink into `quarantine/<batch>/…` and records an undo manifest. The
content-addressed **object** and the **source** are never touched — the picture
is still fully in the vault, just filed under `quarantine/`. `ark quarantine
undo <batch>` (or `undo all`) puts everything back; a rescan won't
un-quarantine. Nothing is ever hard-deleted. Preview with `--dry-run`.

## Offline & private

Reverse geocoding uses a **bundled** city dataset (`ark/geodata/cities.csv`) with
zero network calls. For full worldwide coverage, regenerate that CSV from a
GeoNames dump via `tools/make_geodata.py` — no code changes needed.

## Tech

Python 3.11+ · Pillow + piexif (EXIF, perceptual hashing, blur) · stdlib
`sqlite3` + FTS5 · `hashlib` (blake2b) · `zoneinfo`. Rule-based metadata and
perceptual near-dup/blur are the always-on core and need no AI; an optional
semantic layer (CLIP tags, faces) can plug into `enrich.py` later. SQLite now;
Postgres + pgvector is the documented scale path.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers the sacred rule (source never mutated, no writes outside the vault, dry-run
is write-free, corrupt-copy fails soft), dedup/idempotent rescans, the rule
sandbox (escape attempts rejected), the full end-to-end pipeline, the perceptual
primitives, and the quarantine round-trip (non-destructive move, rescan
stickiness, exact undo).

## Roadmap

- **P1 (done)** — vault core: extract + enrich + organize + versioned safe backup.
- **P2 (done)** — NL search, hash dedup, cleanup intelligence, **near-duplicate +
  blur detection** (`ark similar`) and **reversible quarantine** (`ark quarantine`).
- **P3 (done)** — auto-ingest on device/SSD mount (`ark watch`; stdlib polling, FSEvents-ready).
- **P4** — companion apps (Android background sync; iOS 26.1 photo extension). *Needs mobile toolchains/
  devices — the vault + CLI are the stable contract they target.*
- **P5 (done)** — reasoning layer (`ark insights`): precious-vs-junk keep-score, missing-backup
  (single-copy), cross-device coverage + capture-gap detection.
