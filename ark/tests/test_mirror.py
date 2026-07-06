"""Off-site mirror: verified replication, soft-fail when unreachable, catch-up,
and the non-destructive guarantee (the mirror only ever gains objects)."""

from pathlib import Path

import pytest

from ark import pipeline
from ark.config import default_config
from ark.cli import cmd_init
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database
from ark.hashing import hash_file
from ark.mirror import Mirror
import argparse


def _dump(tmp_path):
    d = tmp_path / "dump"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("alpha content")
    (d / "b.txt").write_text("beta content")
    (d / "sub" / "c.txt").write_text("gamma content")
    return d


def _init(tmp_path, backup=None):
    vault = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(vault), backup=backup, json=True))
    return vault


def _object_names(root: Path) -> set:
    o = root / "objects"
    return {p.name for p in o.rglob("*") if p.is_file()} if o.exists() else set()


def test_mirror_replicates_every_object_verified(tmp_path):
    dump = _dump(tmp_path)
    mirror_dir = tmp_path / "mirror"
    vault = _init(tmp_path, backup=str(mirror_dir))
    cfg = default_config(vault, backup_path=str(mirror_dir))

    stats = pipeline.run(dump, vault, cfg, dry_run=False)
    assert stats.stored == 3 and stats.mirrored == 3 and stats.mirror_failed == 0
    # every vault object exists at the mirror, byte-identical
    assert _object_names(mirror_dir) == _object_names(vault)
    for obj in (vault / "objects").rglob("*"):
        if obj.is_file():
            twin = mirror_dir / obj.relative_to(vault)
            assert twin.is_file() and hash_file(twin) == obj.stem
    # sources untouched
    assert (dump / "a.txt").read_text() == "alpha content"


def test_mirror_disabled_when_target_is_the_vault(tmp_path):
    vault = _init(tmp_path, backup=None)          # defaults backup.path -> the vault
    cfg = default_config(vault)
    assert Mirror.from_config(cfg, vault).enabled is False


def test_mirror_soft_fails_when_unreachable(tmp_path):
    dump = _dump(tmp_path)
    blocker = tmp_path / "blocker"                 # a FILE where the mirror root should be
    blocker.write_text("not a directory")
    vault = _init(tmp_path, backup=str(blocker))
    cfg = default_config(vault, backup_path=str(blocker))

    stats = pipeline.run(dump, vault, cfg, dry_run=False)
    # the primary backup still fully succeeded; the mirror just couldn't be written
    assert stats.stored == 3 and stats.mirror_failed == 3 and stats.mirrored == 0
    assert len(_object_names(vault)) == 3
    assert (dump / "a.txt").read_text() == "alpha content"    # non-destructive regardless


def test_mirror_catches_up_after_the_fact(tmp_path):
    dump = _dump(tmp_path)
    vault = _init(tmp_path)                        # scanned with NO mirror first
    pipeline.run(dump, vault, default_config(vault), dry_run=False)

    mirror_dir = tmp_path / "later-mirror"
    mirror = Mirror.from_config(default_config(vault, backup_path=str(mirror_dir)), vault)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]
    st = mirror.sync(vault, objs)
    assert st.replicated == 3 and st.failed == 0
    assert _object_names(mirror_dir) == _object_names(vault)
    # a second sync is idempotent — everything already present, nothing re-copied
    st2 = mirror.sync(vault, objs)
    assert st2.replicated == 0 and st2.already_present == 3


def test_mirror_verify_detects_a_corrupt_copy(tmp_path):
    dump = _dump(tmp_path)
    mirror_dir = tmp_path / "mirror"
    vault = _init(tmp_path, backup=str(mirror_dir))
    cfg = default_config(vault, backup_path=str(mirror_dir))
    pipeline.run(dump, vault, cfg, dry_run=False)

    mirror = Mirror.from_config(cfg, vault)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]
    assert mirror.verify(objs) == []
    # tamper with one mirror object
    victim = next(p for p in (mirror_dir / "objects").rglob("*") if p.is_file())
    victim.write_bytes(b"corrupted")
    problems = mirror.verify(objs)
    assert len(problems) == 1 and "corrupt" in problems[0]
