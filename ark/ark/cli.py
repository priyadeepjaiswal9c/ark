"""ARK command line — clean, utilitarian, trustworthy.

Commands:
  ark init    [--vault DIR] [--backup DIR]      create/point at a vault
  ark scan    SOURCE [--vault DIR] [--dry-run]  ingest+organize+back up a dump
  ark status  [--vault DIR]                      vault stats
  ark search  QUERY... [--vault DIR] [--limit N] natural-language-ish search
  ark cleanup [--vault DIR]                       safe-to-delete-from-phone report
  ark similar [--vault DIR] [--distance N]        near-duplicate + blurry-photo report
  ark insights [--vault DIR]                       reasoning: precious-vs-junk, missing-backup, per-device
  ark quarantine ACTION [BATCH] [--dry-run]       reversibly declutter (near-duplicates/blurry/…) + undo
  ark rules   [--vault DIR] [--validate]          show/validate organization rules
  ark verify  [--vault DIR]                        re-hash every object (integrity)
  ark mirror  [--vault DIR] [--init|--verify]      initialize/replicate/verify the off-site mirror
  ark watch   [--vault DIR] [--once] [--dry-run]   auto-ingest volumes as they connect
  ark serve   [--vault DIR] [--host H] [--port N]  phone sync receiver + web companion app
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
    similar as similar_mod, quarantine as quarantine_mod, watch as watch_mod,
    insights as insights_mod,
)
from .mirror import Mirror, discover_vault_objects


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

    pin = sub.add_parser("insights", help="reasoning: precious-vs-junk, missing-backup, per-device coverage")
    _vault_arg(pin)
    pin.add_argument("--limit", type=int, default=15, help="max items per section")

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

    pm = sub.add_parser("mirror", help="replicate the object store to the off-site [backup] target")
    _vault_arg(pm)
    mmode = pm.add_mutually_exclusive_group()
    mmode.add_argument("--init", action="store_true",
                       help="explicitly initialize the connected mirror target")
    mmode.add_argument("--verify", action="store_true",
                       help="check the mirror instead of syncing to it")

    pw = sub.add_parser("watch", help="auto-ingest volumes as they connect (non-destructive)")
    _vault_arg(pw)
    pw.add_argument("--once", action="store_true", help="sweep currently-mounted volumes once, then exit")
    pw.add_argument("--interval", type=float, default=None, help="seconds between mount-root polls")
    pw.add_argument("--mount-root", default=None, help="where volumes mount (default: config / /Volumes)")
    pw.add_argument("--dry-run", action="store_true", help="preview scans — writes nothing")

    psv = sub.add_parser("serve", help="run the phone sync receiver + web companion app")
    _vault_arg(psv)
    psv.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 to reach from your phone)")
    psv.add_argument("--port", type=int, default=7777, help="port (default 7777)")
    psv.add_argument("--token", default=None, help="access token (default: a fresh random one)")
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
    if args.cmd == "insights":
        return cmd_insights(args)
    if args.cmd == "quarantine":
        return cmd_quarantine(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "mirror":
        return cmd_mirror(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    if args.cmd == "serve":
        return cmd_serve(args)
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
        if c.get("mirrored") or c.get("mirror_failed"):
            extra = f"   ({c['mirror_failed']} failed — run `ark mirror` to catch up)" if c.get("mirror_failed") else ""
            print(f"  mirrored      : {c['mirrored']}   (replicated off-site){extra}")
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
        rep = similar_mod.analyze_vault(
            db, vault, distance=distance, blur_threshold=blur_threshold)

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
    print("No files were moved or deleted. To reversibly declutter (with full undo):")
    print("  ark quarantine near-duplicates      ark quarantine blurry")
    return 0


def cmd_insights(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    with Database(vault / DIR_META / DB_FILENAME, read_only=True) as db:
        rep = insights_mod.analyze_vault(db, vault)

    if args.json:
        print(json.dumps({
            "summary": rep.summary(), "buckets": rep.buckets,
            "junk": [{"path": k.display, "score": k.score, "reasons": k.reasons} for k in rep.junk],
            "precious_single_copy": [{"path": k.display, "score": k.score} for k in rep.precious_single_copy],
            "devices": [{"device": d.device, "count": d.count, "first": d.first, "last": d.last,
                         "largest_gap_days": d.largest_gap_days, "gap_between": d.gap_between}
                        for d in rep.devices],
        }, indent=2))
        return 0

    b = rep.buckets
    print("🧠 ARK insights — reasoning over your vault (advisory; nothing is changed)\n")
    print(f"  keep-score buckets:  {b[insights_mod.PRECIOUS]} precious · "
          f"{b[insights_mod.NORMAL]} normal · {b[insights_mod.JUNK]} likely junk\n")

    if rep.precious_single_copy:
        print(f"  ⭐ precious, but only ONE copy exists — mirror these off-site "
              f"({len(rep.precious_single_copy)}):")
        for k in _cap(rep.precious_single_copy, args.limit):
            print(f"      {k.score:3}  {k.display}")
        print()
    if rep.junk:
        print(f"  🗑  likely junk — review, then maybe `ark quarantine` ({len(rep.junk)}):")
        for k in _cap(rep.junk, args.limit):
            why = "; ".join(r for r in k.reasons if r.startswith("-")) or "low signal"
            print(f"      {k.score:3}  {k.display}")
            print(f"           ↳ {why}")
        print()
    if rep.devices:
        print("  📷 per-device coverage (gaps hint at un-backed-up stretches):")
        for d in rep.devices:
            gap = ""
            if d.largest_gap_days >= insights_mod._GAP_ALERT_DAYS and d.gap_between:
                gap = f"  ⚠ {d.largest_gap_days}-day gap {d.gap_between[0]}→{d.gap_between[1]}"
            span = f"{d.first or '?'} … {d.last or '?'}"
            print(f"      {d.count:4}  {d.device:24}  {span}{gap}")
        print()
    print(rep.summary())
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
        return 1 if problems else 0
    elif problems:
        _err(f"✗ integrity check FAILED — {len(problems)} problem(s):")
        for p in problems:
            print(f"    {p['path']}  — {p['problem']}")
        print("  Fix: re-run `ark scan <original source>` — ARK self-heals objects it can re-derive.")
        return 1
    else:
        print("✓ integrity OK — every stored object re-hashes to its content address")
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault}")
        return 1
    cfg = load_config(vault)
    mirror = Mirror.from_config(cfg, vault)
    if not mirror.enabled:
        msg = (f"no off-site mirror configured. Set [backup] path in "
               f"{vault / DIR_META / CONFIG_FILENAME} to an external SSD / NAS mount "
               f"(different from the vault), then re-run `ark mirror`.")
        if cfg.backup.kind == "cloud":
            msg = "cloud mirror targets aren't supported yet (no object-store adapter shipped)."
        if args.json:
            print(json.dumps({"enabled": False, "message": msg}))
        else:
            print(msg)
        return 0

    if getattr(args, "init", False):
        problem = mirror.initialize(vault)
        if args.json:
            print(json.dumps({"target": str(mirror.root), "initialized": not problem,
                              "problem": problem}, indent=2))
        elif problem:
            _err(f"could not initialize mirror at {mirror.root}: {problem}")
        else:
            print(f"✓ mirror target initialized at {mirror.root}")
            print("  Run `ark mirror` to replicate the vault objects.")
        return 1 if problem else 0

    with Database(vault / DIR_META / DB_FILENAME) as db:
        db_objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]
    objs = discover_vault_objects(vault, db_objs)

    if args.verify:
        problems = mirror.verify(objs)
        if args.json:
            print(json.dumps({"target": str(mirror.root), "objects": len(objs),
                              "ok": not problems, "problems": problems}, indent=2))
            return 1 if problems else 0
        elif problems:
            _err(f"✗ mirror check FAILED — {len(problems)} problem(s) at {mirror.root}:")
            for p in problems[:50]:
                print(f"    {p}")
            print("  Fix: `ark mirror` to re-replicate missing/corrupt objects.")
            return 1
        else:
            print(f"✓ mirror OK — all {len(objs)} objects present + verified at {mirror.root}")
        return 0

    st = mirror.sync(vault, objs)
    if args.json:
        print(json.dumps({"target": str(mirror.root), "replicated": st.replicated,
                          "already_present": st.already_present, "failed": st.failed,
                          "missing_source": st.missing_source, "problems": st.problems}, indent=2))
        return 1 if st.problems else 0
    print(f"🪞  mirror → {mirror.root}")
    print(f"    {st.replicated} newly replicated · {st.already_present} already present · "
          f"{st.failed} failed")
    if st.problems:
        for p in st.problems[:20]:
            print(f"    ⚠ {p}")
        if mirror.root and not mirror.root.exists():
            print("    (mirror target not reachable — reconnect the drive/NAS and re-run)")
        return 1
    print(f"    ✓ {len(objs)} objects are now in two places.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import serve as serve_mod
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault} — run `ark init --vault {vault}` first")
        return 1
    cfg = load_config(vault)
    try:
        server = serve_mod.make_server(vault, cfg, args.host, args.port, token=args.token)
    except OSError as e:
        _err(f"could not bind {args.host}:{args.port} — {e}")
        return 1
    port = server.server_address[1]
    shown_host = serve_mod.lan_ip() if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{shown_host}:{port}/"
    warning = "TRUSTED LAN ONLY: plain HTTP, no TLS; network observers can capture the bearer token."
    if args.json:
        print(json.dumps({"url": url, "token": server.token, "vault": str(vault),
                          "warning": warning if args.host in ("0.0.0.0", "") else ""}), flush=True)
    else:
        print("📡  ARK sync server — the web companion for your phone")
        print(f"    vault : {vault}")
        print(f"    open on your phone (same Wi-Fi):  {url}")
        print(f"    enter this token in the page:     {server.token}")
        if args.host in ("0.0.0.0", ""):
            print(f"    ⚠ {warning}")
        elif args.host in ("127.0.0.1", "localhost"):
            print("    (bound to localhost; add --host 0.0.0.0 only on a trusted LAN)")
        print("    uploads are ingested non-destructively; Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import dataclasses
    vault = Path(args.vault).expanduser().resolve()
    if not _vault_ready(vault):
        _err(f"no vault at {vault} — run `ark init --vault {vault}` first")
        return 1
    cfg = load_config(vault)
    wc = cfg.watch
    if not wc.enabled:
        msg = "watch is disabled by [watch] enabled=false in the vault config"
        if args.json:
            print(json.dumps({"enabled": False, "message": msg}))
        else:
            print(msg)
        return 0
    overrides = {}
    if args.interval is not None:
        overrides["interval"] = args.interval
    if args.mount_root is not None:
        overrides["mount_root"] = args.mount_root
    if overrides:
        wc = dataclasses.replace(wc, **overrides)

    def scan_one(name: str, vol_path: Path) -> watch_mod.ScanEvent:
        try:
            stats = pipeline.run(vol_path, vault, cfg, dry_run=args.dry_run)
            c = stats.as_counts()
            detail = (f"{c['stored']} new · {c['duplicates']} dup · "
                      f"{c['unchanged']} unchanged · {c['failed']} failed"
                      f"{'  (dry-run)' if args.dry_run else ''}")
            # Any failed file keeps the volume retryable on the next sweep.
            return watch_mod.ScanEvent(name, str(vol_path), c["failed"] == 0, detail)
        except Exception as e:  # a bad volume must never kill the watcher
            return watch_mod.ScanEvent(name, str(vol_path), False, f"scan error: {e}")

    def on_event(e: watch_mod.ScanEvent) -> None:
        if args.json:
            print(json.dumps({"volume": e.volume, "path": e.path,
                              "scanned": e.scanned, "detail": e.detail}), flush=True)
        elif e.scanned:
            print(f"  ⤵ ingested  {e.volume:24} {e.detail}", flush=True)
        else:
            print(f"  · skipped   {e.volume:24} {e.detail}", flush=True)

    if not args.json:
        mode = "once" if args.once else f"every {wc.interval:g}s"
        print(f"👁  ARK watch — {wc.mount_root} → {vault}")
        print(f"    allowlist: {list(wc.sources)}   mode: {mode}"
              f"{'   (dry-run)' if args.dry_run else ''}")
        if not args.once:
            print("    Ctrl-C to stop.\n")
        else:
            print("")

    try:
        events = watch_mod.watch_loop(wc, scan_one, on_event=on_event, once=args.once)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 0
    if args.once and not args.json and not events:
        print("  (no matching volumes mounted right now)")
    return 0


# ---- helpers ---------------------------------------------------------------

def _vault_ready(vault: Path) -> bool:
    return (vault / DIR_META / DB_FILENAME).exists()


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
