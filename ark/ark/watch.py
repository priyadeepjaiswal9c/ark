"""Auto-ingest on device/SSD connect (P3).

When you plug in your phone, camera SD card or backup SSD, ARK notices the new
volume and runs a normal — fully non-destructive — scan of it into the vault.
No daemon framework, no heavy deps: it polls the mount root (``/Volumes`` on
macOS) on an interval, which is portable and trivially testable. A proper
FSEvents/udev binding can slot in later behind the same ``list_volumes`` seam
without touching the decision logic.

Design seams (all injectable, so the loop is unit-testable without real mounts
or real time): ``list_fn`` enumerates volumes, ``scan_fn`` ingests one, and
``sleep_fn`` advances time. The *decision* — which newly-appeared volumes to
scan — is a pure function (``targets_to_scan``).

Safety: scanning is the same non-destructive ``pipeline.run`` used everywhere —
sources are read-only, everything is content-addressed and hash-verified, and a
rescan of an already-ingested volume is idempotent. So auto-ingest can never
lose or mutate anything; the worst case is redundant work (deduped away).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Optional


@dataclass
class WatchConfig:
    enabled: bool = True
    interval: float = 5.0        # seconds between mount-root polls
    debounce: float = 3.0        # settle time after a volume appears, before scanning
    mount_root: str = "/Volumes"
    # Explicit opt-in: mutable display names are not an identity boundary, so
    # auto-ingest nothing until the user supplies exact/pinned volume patterns.
    sources: tuple[str, ...] = ()               # fnmatch patterns of volume names to ingest
    ignore: tuple[str, ...] = (                 # never auto-ingest these (system volumes)
        "Macintosh HD", "Macintosh HD*", "Recovery", "Preboot", "VM", "Data",
        "com.apple.*", "Time Machine*", ".*",
    )
    subpath: str = ""            # optional subdir within a volume to scan (e.g. "DCIM")


@dataclass
class ScanEvent:
    volume: str
    path: str
    scanned: bool
    detail: str = ""


# ---- pure decision logic (unit-tested without any real filesystem) ---------

def selectable(name: str, sources, ignore) -> bool:
    """Should a volume named ``name`` be auto-ingested? Ignore always wins."""
    if any(fnmatch(name, pat) for pat in ignore):
        return False
    return any(fnmatch(name, pat) for pat in sources)


def targets_to_scan(current: set[str], seen: set[str], wc: WatchConfig) -> list[str]:
    """Volumes that are newly present (in ``current``, not in ``seen``) and pass
    the allowlist — the ones a sweep should scan. Deterministically ordered."""
    return sorted(n for n in (current - seen) if selectable(n, wc.sources, wc.ignore))


# ---- volume enumeration (the one OS-specific seam) -------------------------

def list_volumes(mount_root: str = "/Volumes") -> set[str]:
    """Names of currently-mounted volumes under ``mount_root``.

    On macOS every removable volume appears as a directory in ``/Volumes``.
    Missing root or a permissions hiccup yields an empty set, never an error —
    the watcher must keep running."""
    root = Path(mount_root)
    try:
        return {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()}
    except OSError:
        return set()


# ---- the sweep + loop ------------------------------------------------------

def _volume_path(wc: WatchConfig, name: str) -> Path:
    root = Path(wc.mount_root).resolve()
    volume = (root / name).resolve()
    try:
        volume.relative_to(root)
    except ValueError as e:
        raise ValueError(f"volume path escapes mount root: {name}") from e
    if not wc.subpath:
        return volume
    sub = Path(wc.subpath)
    if sub.is_absolute() or ".." in sub.parts:
        raise ValueError(f"watch subpath must be relative and contain no '..': {wc.subpath}")
    target = (volume / sub).resolve()
    try:
        target.relative_to(volume)
    except ValueError as e:
        raise ValueError(f"watch subpath escapes volume: {wc.subpath}") from e
    return target


def sweep(
    wc: WatchConfig,
    seen: set[str],
    scan_fn: Callable[[str, Path], ScanEvent],
    list_fn: Callable[[str], set[str]] = list_volumes,
    sleep_fn: Callable[[float], None] = None,
    debounce: bool = True,
) -> list[ScanEvent]:
    """One pass: find newly-appeared allowlisted volumes, (optionally) wait out
    the debounce, confirm they're still mounted, and scan each. Mutates ``seen``
    to include every volume observed this pass (so vanished-then-returned media
    is re-scanned, and a volume is never scanned twice while it stays mounted).
    """
    current = list_fn(wc.mount_root)
    targets = targets_to_scan(current, seen, wc)
    events: list[ScanEvent] = []

    if targets and debounce and wc.debounce > 0 and sleep_fn is not None:
        sleep_fn(wc.debounce)
        current = list_fn(wc.mount_root)      # re-read: a flaky/brief mount drops out here

    for name in targets:
        try:
            vol_path = _volume_path(wc, name)
        except ValueError as e:
            events.append(ScanEvent(name, "", False, str(e)))
            continue
        if name not in current:                       # confirmed unmounted by list_fn
            events.append(ScanEvent(name, str(vol_path), False,
                                    "disappeared before it settled"))
            continue
        # The volume is present (list_fn just reported it); only a configured
        # subpath needs a disk check — e.g. a card with no DCIM/ folder.
        if wc.subpath and not vol_path.exists():
            events.append(ScanEvent(name, str(vol_path), False, "scan subpath not present"))
            continue
        events.append(scan_fn(name, vol_path))

    # Keep successful and previously-seen mounts. A failed/not-ready target is
    # deliberately NOT marked seen, so the next sweep retries it while mounted.
    successful = {e.volume for e in events if e.scanned}
    attempted = set(targets)
    next_seen = (seen & current) | (current - attempted) | successful
    seen.clear()
    seen.update(next_seen)
    return events


def watch_loop(
    wc: WatchConfig,
    scan_fn: Callable[[str, Path], ScanEvent],
    on_event: Optional[Callable[[ScanEvent], None]] = None,
    list_fn: Callable[[str], set[str]] = list_volumes,
    sleep_fn: Callable[[float], None] = None,
    once: bool = False,
    scan_existing: bool = True,
    max_iterations: Optional[int] = None,
) -> list[ScanEvent]:
    """Poll the mount root until interrupted.

    ``scan_existing`` (default) treats volumes already mounted at startup as new
    — plug your SSD in, start ``ark watch``, and it ingests what's there, then
    keeps watching. ``once`` does a single sweep and returns. ``max_iterations``
    bounds the loop (tests). Returns all events (handy for ``--once``/tests)."""
    import time
    sleep_fn = sleep_fn or time.sleep

    seen: set[str] = set() if scan_existing else set(list_fn(wc.mount_root))
    all_events: list[ScanEvent] = []
    iterations = 0
    while True:
        events = sweep(wc, seen, scan_fn, list_fn=list_fn, sleep_fn=sleep_fn,
                       debounce=not once)
        for e in events:
            all_events.append(e)
            if on_event:
                on_event(e)
        iterations += 1
        if once or (max_iterations is not None and iterations >= max_iterations):
            break
        sleep_fn(wc.interval)
    return all_events
