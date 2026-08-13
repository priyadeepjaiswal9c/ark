# ARK — Intelligent, Non-Destructive Backup Vault

ARK turns an unstructured device or SSD dump into a verified, searchable vault—without modifying the source. It combines content-addressed storage, hash deduplication, metadata enrichment, safe cleanup evidence, reversible quarantine, and off-site mirroring in one local-first CLI.

> **Core promise:** sources are read-only, copies are hash-verified, and cleanup remains explainable and reversible.

## Why it exists

Conventional backup tools answer “did a copy run?” ARK answers the harder questions:

- Is this exact file safely stored?
- Which copies are duplicates or near-duplicates?
- What is genuinely safe to remove from a phone?
- Is the second backup independent and intact?
- Can the archive still be searched by date, place, type, or description?

## How it works

```text
source device (read-only)
        ↓ extract + hash + enrich
content-addressed object store ──→ human-readable organized view
        │                                  │
        ├── SQLite/FTS search              └── reversible quarantine
        └── verified independent mirror
```

Identical bytes share one object. The organized view uses hardlinks, so one file can appear in useful locations without consuming duplicate storage. New objects are written atomically and re-hashed before commit.

## Capabilities

- Dry-run and idempotent ingestion
- BLAKE2b content identity and exact deduplication
- EXIF extraction, offline reverse geocoding, and rule-based organization
- Natural-language-style and structured search
- Perceptual duplicate and blur detection
- Evidence-backed phone cleanup reports
- Reversible quarantine with undo manifests
- Mount-aware external mirror initialization and verification
- Local phone upload receiver with an installable PWA

## Quick start

```bash
cd ark
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

ark init
ark scan /path/to/source --dry-run
ark scan /path/to/source
ark status
ark verify
```

The complete command reference, security model, vault layout, organization rules, and mirror workflow live in the [ARK technical guide](ark/README.md).

## Repository map

| Path | Purpose |
|---|---|
| [`ark/`](ark/) | Python package, CLI, database, rules, storage, and PWA |
| [`ark/tests/`](ark/tests/) | Safety, integrity, pipeline, search, mirror, and quarantine tests |
| [`ark/ark.example.toml`](ark/ark.example.toml) | Example configuration |
| [`ark/README.md`](ark/README.md) | Full technical documentation |

## Verification

```bash
cd ark
python -m pytest -q
```

The tests cover source immutability, dry-run behavior, path containment, hash verification, deduplication, idempotent rescans, rule sandboxing, mirror safety, and quarantine round-trips.

## Contributors

| Contributor | Role |
|---|---|
| [**Priyadeep Jaiswal**](https://github.com/priyadeepjaiswal9c) | Owner; product design, architecture, implementation, and testing |

Priyadeep defined the product constraints, integrated the system, validated its safety properties, and owns the resulting work.
