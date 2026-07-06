"""ARK command line — clean, utilitarian, trustworthy.

Commands:
  ark init    [--vault DIR] [--backup DIR]      create/point at a vault
  ark scan    SOURCE [--vault DIR] [--dry-run]  ingest+organize+back up a dump
  ark status  [--vault DIR]                      vault stats
  ark search  QUERY... [--vault DIR] [--limit N] natural-language-ish search
  ark cleanup [--vault DIR]                       safe-to-delete-from-phone report
  ark similar [--vault DIR] [--distance N]        near-duplicate + blurry-photo report
  ark quarantine ACTION [BATCH] [--dry-run]       reversibly declutter (near-duplicates/blurry/…) + undo
  ark rules   [--vault DIR] [--validate]          show/validate organization rules
  ark verify  [--vault DIR]                        re-hash every object (integrity)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .config import Config, default_config, load_config, render_config_toml
from .constants import (
    CONFIG_FILENAME, DB_FILENAME, DIR_META, DIR_OBJECTS, DIR_ORGANIZED,
    DIR_QUARANTINE,
)
from .db import Database
from .backup import VaultWriter
from . import (
    pipeline, report, rules as rules_mod, search as search_mod,
    similar as similar_mod, quarantine as quarantine_mod,
)


def _vault_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--vault", default=os.environ.get("ARK_VAULT", str(Path.cwd() / "ark-vault")),
        help="vault directory (default: $ARK_VAULT or ./ark-vault)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ark", description="ARK — the intelligent vault for your digital life.")
    p.add_argument("--version", action="version", version=f"ark {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create a vault")
    _vault_arg(pi)
    pi.add_argument("--backup", default=None, help="backup target path (default: the vault itself)")

    ps = sub.add_parser("scan", help="ingest, organize and back up a source dump")
    ps.add_argument("source", help="folder (or file) to ingest")
    _vault_arg(ps)
    ps.add_argument("--dry-run", action="store_true", help="preview only — zero writes")
    ps.add_argument("-q", "--quiet", action="store_true", help="suppress per-file lines")

    pst = sub.add_parser("status", help="vault statistics")
    _vault_arg(pst)

    pse = sub.add_parser("search", help="search the vault")
    pse.add_argument("query", nargs="+", help="query terms and filters (kind:, place:, year:)")
    _vault_arg(pse)
    pse.add_argument("--limit", type=int, default=25)

    pc = sub.add_parser("cleanup", help="safe-to-delete-from-phone report")
    _vault_arg(pc)

    psi = sub.add_parser("similar", help="find near-duplicate and blurry photos")
    _vault_arg(psi)
    psi.add_argument("--distance", type=int, default=None,
                     help="max perceptual Hamming distance for 'same picture' (default 10)")
    psi.add_argument("--blur-threshold", type=float, default=None,
                     help="Laplacian-variance below which a photo is 'blurry' (default 60)")

    pq = sub.add_parser(
        "quarantine",
        help="reversibly move redundant/blurry organized entries into quarantine/ (with undo)")
    _vault_arg(pq)
    pq.add_argument("action", choices=["duplicates", "near-duplicates", "blurry", "list", "undo"],
                    help="what to quarantine, or 'list' / 'undo'")
    pq.add_argument("batch", nargs="?", help="batch id for `undo` (or 'all')")
    pq.add_argument("--dry-run", action="store_true", help="preview only — move nothing")
    pq.add_argument("--distance", type=int, default=None, help="near-duplicate Hamming distance")
    pq.add_argument("--blur-threshold", type=float, default=None, help="blur variance threshold")

    pr = sub.add_parser("rules", help="show/validate organization rules")
    _vault_arg(pr)
    pr.add_argument("--validate", action="store_true")

    pv = sub.add_parser("verify", help="re-hash every stored object (integrity check)")
    _vault_arg(pv)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130
    except sqlite3.DatabaseError as e:
        _err(f"vault database problem: {e}. Try `ark verify` or restore .ark/ark.db from a backup.")
        return 1
    except Exception as e:  # a user tool should fail with a message, not a traceback
        _err(f"{type(e).__name__}: {e}")
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "cleanup":
        return cmd_cleanup(args)
    if args.cmd == "similar":
        return cmd_similar(args)
    if args.cmd == "quarantine":
        return cmd_quarantine(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    return 2


# ---- commands --------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    for d in (DIR_OBJECTS, DIR_ORGANIZED, DIR_QUARANTINE, DIR_META):
        (vault / d).mkdir(parents=True, exist_ok=True)
    cfg_path = vault / DIR_META / CONFIG_FILENAME
    if not cfg_path.exists():
        cfg = default_config(vault, args.backup)
        cfg_path.write_text(render_config_toml(cfg), encoding="utf-8")
    # touch the DB (creates schema)
    Database(vault / DIR_META / DB_FILENAME).close()
    cfg = load_config(vault)
    if args.json:
        print(json.dumps({"vault": str(vault), "config": str(cfg_path),
                          "backup": {"kind": cfg.backup.kind, "path": cfg.backup.path},
                          "rules": len(cfg.rules)}, indent=2))
    else:
        print(f"✓ vault ready at {vault}")
        print(f"  backup target : {cfg.backup.kind} → {cfg.backup.path}")
        print(f"  config        : {cfg_path}")
        print(f"  rules         : {len(cfg.rules)} loaded")
        print("\nNext:  ark scan <your-dump-folder> --dry-run --vault " + str(vault))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault} — run `ark init --vault {vault}` first")
        return 1
    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        _err(f"source not found: {source}")
        return 1
    cfg = load_config(vault)
    errs = rules_mod.validate_rules(cfg)
    if errs:
        _err("invalid rules:\n  " + "\n  ".join(errs))
        return 1

    mode = "DRY-RUN (no writes)" if args.dry_run else "COMMIT"
    if not args.json:
        print(f"⟳ scanning {source}\n  vault: {vault}\n  mode : {mode}\n")

    def progress(item: pipeline.ItemResult) -> None:
        if args.json or args.quiet:
            return
        if item.unchanged:
            icon, label = "·", "unchanged"
        elif item.healed:
            icon, label = "♺", "healed"
        else:
            icon = {"stored": "＋", "duplicate": "≡", "needs_review": "?",
                    "failed": "✗"}.get(item.status, "·")
            label = item.status
        rel = item.organized_path or "-"
        print(f"  {icon} {label:12} {rel:40} ← {Path(item.source_path).name}")

    stats = pipeline.run(source, vault, cfg, dry_run=args.dry_run, progress=progress)

    if args.json:
        print(json.dumps(stats.as_counts(), indent=2))
    else:
        c = stats.as_counts()
        print("\n── summary ──────────────────────────────")
        print(f"  files seen    : {c['total']}")
        print(f"  newly stored  : {c['stored']}   ({report._human(c['bytes_new'])})")
        print(f"  duplicates    : {c['duplicates']}   ({report._human(c['bytes_dup'])} redundant)")
        if c.get("unchanged"):
            print(f"  unchanged     : {c['unchanged']}   (already stored, nothing written)")
        if c.get("healed"):
            print(f"  healed        : {c['healed']}   (corrupt vault objects rewritten from source)")
        print(f"  needs review  : {c['needs_review']}")
        print(f"  failed        : {c['failed']}")
        if c["by_kind"]:
            kinds = ", ".join(f"{k}:{v}" for k, v in c["by_kind"].items())
            print(f"  by kind       : {kinds}")
        if args.dry_run:
            print("\n  (dry-run — nothing was written. Re-run without --dry-run to commit.)")
        else:
            print(f"\n  ✓ vault updated. Try:  ark search \"...\" --vault {vault}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    with Database(vault / DIR_META / DB_FILENAME) as db:
        s = report.status_report(db)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print(f"ARK vault: {vault}")
        print(f"  assets ingested : {s['assets']}")
        print(f"  distinct objects: {s['objects']}")
        print(f"  stored          : {s['human']['stored']}")
        print(f"  saved by dedup  : {s['human']['saved_by_dedup']}")
        print(f"  duplicates      : {s['duplicates']}")
        print(f"  needs review    : {s['needs_review']}")
        if s["by_kind"]:
            print("  by kind         : " + ", ".join(f"{k}={v}" for k, v in s["by_kind"].items()))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    query = " ".join(args.query)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        hits = search_mod.search(db, query, limit=args.limit)
    if args.json:
        print(json.dumps([h.__dict__ for h in hits], indent=2))
    else:
        print(f"“{query}” → {len(hits)} result(s)\n")
        for h in hits:
            loc = ", ".join(x for x in (h.place_city, h.place_country) if x) or "—"
            when = (h.taken_at or "")[:10] or "—"
            print(f"  {when}  {(h.kind or '?'):8}  {loc:22}  {h.organized_path or Path(h.source_path).name}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = report.cleanup_report(db, vault)
    if args.json:
        print(json.dumps({
            "summary": rep.summary(), "reclaimable_bytes": rep.reclaimable_bytes,
            "exact_duplicates": rep.exact_duplicates,
            "safe_to_delete": [c.__dict__ for c in rep.safe_to_delete],
            "at_risk": [a.__dict__ for a in rep.at_risk],
            "unverifiable": [a.__dict__ for a in rep.unverifiable],
        }, indent=2))
    else:
        print("🧹 Cleanup intelligence — safe to clear from your phone/source")
        print("   (each item: vault object AND live source RE-HASHED just now to prove it)\n")
        dups = [c for c in rep.safe_to_delete if c.is_exact_duplicate]
        singles = [c for c in rep.safe_to_delete if not c.is_exact_duplicate]
        if dups:
            print(f"  exact-duplicate copies ({rep.exact_duplicates} redundant across "
                  f"{len(dups) - rep.exact_duplicates} unique):")
            for c in _cap(dups, 20):
                print(f"    ≡ {report._human(c.size):>9}  {c.source_path}")
        if singles:
            print(f"\n  backed up & clearable from your phone ({len(singles)}):")
            for c in _cap(singles, 20):
                print(f"    ✓ {report._human(c.size):>9}  {c.source_path}")
            if len(singles) > 20:
                print(f"    … and {len(singles) - 20} more (use --json for the full list)")
        if rep.at_risk:
            print("\n  ⚠️  AT RISK — backup unverified or source changed, do NOT delete these:")
            for a in rep.at_risk:
                print(f"    ✗ {report._human(a.size):>9}  {a.source_path}  ({a.problem})")
        if rep.unverifiable:
            print("\n  ?  UNVERIFIABLE — source unreadable now (connect the device, re-run):")
            for a in rep.unverifiable:
                print(f"    ? {report._human(a.size):>9}  {a.source_path}")
        print("\n" + rep.summary())
        print("ARK never deletes for you — a suggestion backed by a fresh hash re-check.")
        print("Tip: keep your vault target itself backed up — after you clear the source,")
        print("     the vault becomes your copy of record.")
    return 0


def _cap(items: list, n: int) -> list:
    return items[:n]


def cmd_similar(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    cfg = load_config(vault)
    distance = args.distance if args.distance is not None else cfg.near_dup_distance
    blur_threshold = args.blur_threshold if args.blur_threshold is not None else cfg.blur_threshold
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = similar_mod.analyze_vault(db, vault, distance=distance, blur_threshold=blur_threshold)

    if args.json:
        print(json.dumps({
            "summary": rep.summary(),
            "distance": rep.distance, "blur_threshold": rep.blur_threshold,
            "images_scanned": rep.images_scanned,
            "near_duplicate_groups": [
                {"keep": g.keeper.display, "reclaimable_bytes": g.reclaimable_bytes,
                 "members": [{"path": m.display, "source": m.source_path, "blur": m.blur,
                              "size": m.size, "keep": m.id == g.keep_id} for m in g.members]}
                for g in rep.near_dup_groups],
            "blurry": [{"path": b.display, "source": b.source_path, "blur": b.blur}
                       for b in rep.blurry],
        }, indent=2))
        return 0

    print("🔎 Near-duplicate & blur scan")
    print(f"   (perceptual dHash ≤ {distance} == same picture; "
          f"Laplacian variance < {blur_threshold:g} == likely blurry)\n")
    if rep.near_dup_groups:
        print(f"  near-duplicate groups ({len(rep.near_dup_groups)}) — visually the same shot, different bytes:")
        for i, g in enumerate(rep.near_dup_groups, 1):
            print(f"    group {i} — {len(g.members)} photos, keep the best copy "
                  f"({report._human(g.reclaimable_bytes)} reclaimable):")
            for m in g.members:
                mark = "★ keep" if m.id == g.keep_id else "  dup "
                sharp = f"{m.blur:7.1f}" if m.blur is not None else "      ?"
                print(f"      {mark}  blur {sharp}  {m.display}")
    else:
        print("  near-duplicate groups: none")
    if rep.blurry:
        print(f"\n  blurry photos ({len(rep.blurry)}) — lowest sharpness first:")
        for b in _cap(rep.blurry, 20):
            print(f"      blur {b.blur:7.1f}  {b.display}")
        if len(rep.blurry) > 20:
            print(f"      … and {len(rep.blurry) - 20} more (use --json for the full list)")
    else:
        print("\n  blurry photos: none")
    print("\n" + rep.summary())
    print("Nothing was changed. To reversibly declutter (with full undo):")
    print("  ark quarantine near-duplicates      ark quarantine blurry")
    return 0


def cmd_quarantine(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    with Database(vault / DIR_META / DB_FILENAME) as db:
        if args.action == "list":
            return _quarantine_list(db, args)
        if args.action == "undo":
            return _quarantine_undo(db, vault, args)
        return _quarantine_add(db, vault, args)


def _quarantine_add(db: Database, vault: Path, args: argparse.Namespace) -> int:
    plan = quarantine_mod.plan(db, vault, args.action,
                               distance=args.distance, blur_threshold=args.blur_threshold)
    res = quarantine_mod.apply(db, vault, plan, dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "action": args.action, "dry_run": args.dry_run, "batch": res.batch,
            "moved": [{"path": c.display, "source": c.source_path, "size": c.size} for c in res.moved],
            "reclaimable_bytes": res.reclaimable_bytes,
            "skipped": [{"path": p, "why": w} for p, w in res.skipped],
        }, indent=2))
        return 0
    verb = "would quarantine" if args.dry_run else "quarantined"
    if not res.moved:
        print(f"nothing to quarantine for '{args.action}'.")
        if res.skipped:
            for p, w in _cap(res.skipped, 20):
                print(f"    · skipped {p}  ({w})")
        return 0
    print(f"🗄  {verb} {len(res.moved)} entr{'y' if len(res.moved) == 1 else 'ies'} "
          f"({report._human(res.reclaimable_bytes)}) — reason: {args.action}")
    if not args.dry_run:
        print(f"   batch: {res.batch}")
    for c in _cap(res.moved, 30):
        print(f"    → {report._human(c.size):>9}  {c.display}")
    if len(res.moved) > 30:
        print(f"    … and {len(res.moved) - 30} more")
    for p, w in res.skipped:
        print(f"    · skipped {p}  ({w})")
    print("\nNo bytes were deleted — the objects and your sources are untouched.")
    if args.dry_run:
        print(f"Re-run without --dry-run to move them into quarantine/.")
    else:
        print(f"Undo anytime:  ark quarantine undo {res.batch}    (or: ark quarantine undo all)")
    return 0


def _quarantine_list(db: Database, args: argparse.Namespace) -> int:
    batches = db.quarantine_batches()
    if args.json:
        print(json.dumps([{"batch": b["batch"], "reason": b["reason"],
                           "active": b["active"], "at": b["at"]} for b in batches], indent=2))
        return 0
    if not batches:
        print("no active quarantine batches. (Everything is in organized/.)")
        return 0
    print(f"active quarantine batches ({len(batches)}):\n")
    for b in batches:
        print(f"  {b['batch']}   {b['reason']:15}  {b['active']} item(s)   {(b['at'] or '')[:19]}")
    print("\nRestore one:  ark quarantine undo <batch>     Restore all:  ark quarantine undo all")
    return 0


def _quarantine_undo(db: Database, vault: Path, args: argparse.Namespace) -> int:
    if not args.batch:
        _err("undo needs a batch id (or 'all'). See `ark quarantine list`.")
        return 1
    results = quarantine_mod.undo(db, vault, args.batch)
    total = sum(len(r.restored) + len(r.relocated) for r in results.values())
    touched = total + sum(len(r.missing) for r in results.values())
    if args.json:
        print(json.dumps({b: {"restored": r.restored, "relocated": r.relocated,
                              "missing": r.missing} for b, r in results.items()}, indent=2))
        return 0
    if touched == 0:
        print(f"no active quarantine batch matching '{args.batch}'. See `ark quarantine list`.")
        return 0
    for b, r in results.items():
        print(f"↩  restored batch {b}: {len(r.restored)} back in place"
              + (f", {len(r.relocated)} relocated (original spot was taken)" if r.relocated else "")
              + (f", {len(r.missing)} missing" if r.missing else ""))
        for want, actual in r.relocated:
            print(f"    ~ {want}  →  {actual}")
    print(f"\n{total} entr{'y' if total == 1 else 'ies'} returned to organized/.")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    cfg = load_config(vault) if _vault_ready(vault) else default_config(vault)
    if args.validate:
        errs = rules_mod.validate_rules(cfg)
        if errs:
            _err("invalid rules:\n  " + "\n  ".join(errs))
            return 1
        print(f"✓ all {len(cfg.rules)} rules valid")
        return 0
    if args.json:
        print(json.dumps([{"name": r.name, "when": r.when, "to": r.to, "tags": r.tags}
                          for r in cfg.rules], indent=2))
    else:
        print(f"organization rules ({len(cfg.rules)}, first match wins):\n")
        for i, r in enumerate(cfg.rules, 1):
            print(f"  {i}. {r.name}")
            print(f"       when: {r.when}")
            print(f"       to  : {r.to}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    cfg = load_config(vault)
    problems = VaultWriter(vault, cfg).verify_vault()
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        _err(f"✗ integrity check FAILED — {len(problems)} problem(s):")
        for p in problems:
            print(f"    {p['path']}  — {p['problem']}")
        print("  Fix: re-run `ark scan <original source>` — ARK self-heals objects it can re-derive.")
        return 1
    else:
        print("✓ integrity OK — every stored object re-hashes to its content address")
    return 0


# ---- helpers ---------------------------------------------------------------

def _vault_ready(vault: Path) -> bool:
    return (vault / DIR_META / DB_FILENAME).exists()


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
