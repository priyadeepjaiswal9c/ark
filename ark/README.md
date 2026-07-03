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
| `ark status` | Vault stats: assets, distinct objects, bytes saved by dedup, per-kind. |
| `ark rules [--validate]` | Show / validate the organization rules. |
| `ark verify` | Re-hash every stored object against its content address (corruption check). |

---

## How the vault is laid out

```
<vault>/
  objects/<ab>/<hash>.<ext>     content-addressed store — each distinct file once
  organized/<rule path>/...     human-readable tree, hardlinked into objects (no data dup)
  quarantine/                   dedup / needs-review pointers (never real deletions)
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

## Offline & private

Reverse geocoding uses a **bundled** city dataset (`ark/geodata/cities.csv`) with
zero network calls. For full worldwide coverage, regenerate that CSV from a
GeoNames dump via `tools/make_geodata.py` — no code changes needed.

## Tech

Python 3.11+ · Pillow + piexif (EXIF) · stdlib `sqlite3` + FTS5 · `hashlib`
(blake2b) · `zoneinfo`. Rule-based metadata is the always-on core and needs no
AI; the optional AI/semantic layer (CLIP tags, near-dup detection) plugs into
`enrich.py` later. SQLite now; Postgres + pgvector is the documented scale path.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers the sacred rule (source never mutated, no writes outside the vault, dry-run
is write-free, corrupt-copy fails soft), dedup/idempotent rescans, the rule
sandbox (escape attempts rejected), and the full end-to-end pipeline.

## Roadmap

- **P1 (done)** — vault core: extract + enrich + organize + versioned safe backup.
- **P2 (groundwork here)** — search, hash dedup + quarantine, cleanup intelligence.
- **P3** — auto-ingest on device/SSD mount (FSEvents watchers).
- **P4** — companion apps (Android background sync; iOS 26.1 photo extension).
- **P5** — reasoning layer: precious-vs-junk, near-dup/blur, missing-backup, cross-device.
