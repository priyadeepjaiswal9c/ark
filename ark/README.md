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
| `ark similar [--distance N] [--blur-threshold X]` | Find **near-duplicate** photos (visually the same shot, different bytes) and **blurry** ones. It never moves/deletes files; older vaults may have missing perceptual signals computed and persisted during the scan. |
| `ark insights` | Reasoning layer: a transparent **keep-score** (precious/normal/junk, every point justified), **missing-backup** (precious single-copy items to mirror), and **per-device** coverage + capture gaps. Read-only. |
| `ark watch [--once] [--interval SEC] [--mount-root DIR] [--dry-run]` | **Auto-ingest on connect** — scans a volume the moment it mounts, matched against a config allowlist. `--interval` overrides the polling period; `--mount-root` overrides the configured mount directory (normally `/Volumes`). Non-destructive; a reconnected card is re-scanned (dedup makes it cheap). |
| `ark mirror [--init\|--verify]` | Replicate the object store to the `[backup]` target (an external SSD / NAS), atomically + hash-verified. After configuring a new target, connect it and run `ark mirror --init` once; normal scans and `ark mirror` then sync, while `--verify` only checks it. |
| `ark serve [--host H] [--port N] [--token TOKEN] [--json]` | **Phone companion.** Runs a tiny sync receiver + installable PWA. `--token` supplies the bearer token instead of generating one; `--json` emits startup details as JSON. Uploads are non-destructive, deduped, and organized. |
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

## Mirror initialization and mount safety

Set `[backup] path` to a directory on a connected external SSD or NAS, then run:

```bash
$PY -m ark mirror --init
$PY -m ark mirror
$PY -m ark mirror --verify
```

Initialization is an explicit, one-time bootstrap. ARK refuses a target that is
the vault, overlaps it, resolves through an unsafe symlink, or is on the same
device. It writes a vault-bound marker only after those checks pass. Every later
mirror write requires that marker, so an absent drive/NAS cannot silently turn
its stale mountpoint into a local directory and be reported as redundant storage.

## Phone receiver security

`ark serve` binds to `127.0.0.1` by default. The web app sends the shared token
in an `Authorization: Bearer ...` header; it is not placed in the URL. Use
`--token TOKEN` to choose it yourself, and `--json` when a launcher needs the
startup URL/token as machine-readable output.

> **Warning:** `ark serve --host 0.0.0.0` is for a **trusted LAN only**. The
> built-in server uses plain HTTP and provides no TLS, so anyone able to observe
> that network traffic can capture the bearer token and uploaded data.

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
sharpest, ties broken by file size) and blurry photos. It never moves or deletes
files; if an older vault lacks perceptual signals, the scan persists that derived
metadata so later scans do not have to recompute it. `ark insights` remains fully
read-only and computes any missing signals only in memory.

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
- **P4 (web companion done)** — `ark serve` runs a phone sync receiver + installable PWA that works on
  Android and iOS through the browser (no app store). Native background-sync apps remain optional and
  need mobile toolchains; they'd target the same HTTP contract.
- **P5 (done)** — reasoning layer (`ark insights`): precious-vs-junk keep-score, missing-backup
  (single-copy), cross-device coverage + capture-gap detection.
