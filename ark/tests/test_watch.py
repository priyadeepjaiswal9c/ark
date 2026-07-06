"""The mount-watcher (P3): allowlist logic, debounce, seen-tracking, and a real
end-to-end auto-ingest of a simulated mounted volume."""

from pathlib import Path

from ark import pipeline, watch as W
from ark.config import default_config
from ark.cli import cmd_init
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database
import argparse


class FakeMounts:
    """Returns a scripted sequence of volume-name sets, one per call (sticking on
    the last), so a sweep/loop can be driven without real mounts."""
    def __init__(self, *frames):
        self.frames = [set(f) for f in frames]
        self.calls = 0

    def __call__(self, _root):
        f = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return set(f)


def _wc(**kw):
    base = dict(mount_root="/V", sources=("*",),
                ignore=("Macintosh HD", "Macintosh HD*", "com.apple.*", ".*"),
                debounce=0.0, interval=0.0)
    base.update(kw)
    return W.WatchConfig(**base)


# ---- pure decision logic ---------------------------------------------------

def test_selectable_ignore_beats_sources():
    assert W.selectable("SonySD", ("*",), ("Macintosh HD*",)) is True
    assert W.selectable("Macintosh HD", ("*",), ("Macintosh HD*",)) is False
    assert W.selectable(".hidden", ("*",), (".*",)) is False
    # sources allowlist can be specific
    assert W.selectable("Pixel", ("Pixel*", "Sony*"), ()) is True
    assert W.selectable("Random USB", ("Pixel*",), ()) is False


def test_targets_only_new_and_allowlisted():
    wc = _wc()
    got = W.targets_to_scan({"SonySD", "Macintosh HD", "Backup"}, {"Backup"}, wc)
    assert got == ["SonySD"]                 # Backup already seen, Macintosh HD ignored


# ---- sweep -----------------------------------------------------------------

def test_sweep_scans_new_and_marks_seen():
    scanned = []
    wc = _wc()
    seen = set()
    events = W.sweep(wc, seen, lambda n, p: W.ScanEvent(n, str(p), True),
                     list_fn=FakeMounts({"SonySD", "Macintosh HD"}))
    assert [e.volume for e in events if e.scanned] == ["SonySD"]
    assert seen == {"SonySD", "Macintosh HD"}    # everything present is now seen
    # a second sweep with no change scans nothing
    again = W.sweep(wc, seen, lambda n, p: scanned.append(n) or W.ScanEvent(n, str(p), True),
                    list_fn=FakeMounts({"SonySD", "Macintosh HD"}))
    assert again == [] and scanned == []


def test_debounce_drops_a_flaky_mount():
    wc = _wc(debounce=3.0)
    slept = []
    # first read sees the flaky card; the post-debounce re-read no longer does
    lister = FakeMounts({"FlakySD"}, set())
    events = W.sweep(wc, set(), lambda n, p: W.ScanEvent(n, str(p), True),
                     list_fn=lister, sleep_fn=lambda s: slept.append(s))
    assert slept == [3.0]                         # we waited out the debounce
    assert len(events) == 1 and events[0].scanned is False
    assert "disappeared" in events[0].detail


def test_reconnect_triggers_a_fresh_scan():
    wc = _wc()
    calls = []
    scan = lambda n, p: calls.append(n) or W.ScanEvent(n, str(p), True)
    seen = set()
    W.sweep(wc, seen, scan, list_fn=FakeMounts({"SonySD"}))     # connect -> scan
    W.sweep(wc, seen, scan, list_fn=FakeMounts(set()))          # unmount -> forgotten
    W.sweep(wc, seen, scan, list_fn=FakeMounts({"SonySD"}))     # reconnect -> scan again
    assert calls == ["SonySD", "SonySD"]


# ---- loop ------------------------------------------------------------------

def test_loop_detects_new_volume_across_iterations():
    wc = _wc()
    # iteration 1 sees nothing new (started with these), iteration 2 a card appears
    lister = FakeMounts(set(), {"SonySD"}, {"SonySD"})
    scan = lambda n, p: W.ScanEvent(n, str(p), True)
    events = W.watch_loop(wc, scan, list_fn=lister, sleep_fn=lambda s: None,
                          scan_existing=True, max_iterations=2)
    assert [e.volume for e in events if e.scanned] == ["SonySD"]


def test_once_is_a_single_sweep():
    wc = _wc()
    lister = FakeMounts({"SonySD"})
    events = W.watch_loop(wc, lambda n, p: W.ScanEvent(n, str(p), True),
                          list_fn=lister, sleep_fn=lambda s: None, once=True)
    assert [e.volume for e in events] == ["SonySD"]
    assert lister.calls == 1                       # exactly one poll, no debounce


# ---- real end-to-end auto-ingest ------------------------------------------

def test_watch_once_ingests_a_mounted_volume(tmp_path):
    # a fake mount root with one "card" holding a couple of files
    mroot = tmp_path / "Volumes"
    card = mroot / "SONY_SD"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "a.txt").write_text("hello from the card")
    (card / "notes.md").write_text("# trip notes")

    vault = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)
    wc = W.WatchConfig(mount_root=str(mroot), sources=("*",), ignore=(".*",), debounce=0.0)

    def scan_one(name, path):
        stats = pipeline.run(path, vault, cfg, dry_run=False)
        return W.ScanEvent(name, str(path), True, f"{stats.stored} new")

    events = W.watch_loop(wc, scan_one, list_fn=W.list_volumes,
                          sleep_fn=lambda s: None, once=True)
    assert [e.volume for e in events] == ["SONY_SD"]
    with Database(vault / DIR_META / DB_FILENAME) as db:
        s = db.stats()
    assert s["assets"] == 2 and s["objects"] == 2      # both files ingested + backed up
    # non-destructive: the card's files are still there, unchanged
    assert (card / "DCIM" / "a.txt").read_text() == "hello from the card"
