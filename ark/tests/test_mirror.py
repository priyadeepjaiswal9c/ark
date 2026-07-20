"""Off-site mirror: verified replication, soft-fail when unreachable, catch-up,
and the non-destructive guarantee (the mirror only ever gains objects)."""

from pathlib import Path

import pytest

from ark import mirror as mirror_mod, pipeline
from ark.config import default_config
from ark.cli import cmd_init, main
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database
from ark.hashing import hash_file
from ark.mirror import MIRROR_MARKER, Mirror, discover_vault_objects
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


def _initialize_mirror(vault: Path, mirror_dir: Path, monkeypatch) -> Mirror:
    # tmp_path puts both directories on one physical filesystem. Model the
    # distinct-device identity of a real external SSD/NAS while exercising the
    # exact production initialization and marker path.
    monkeypatch.setattr(mirror_mod, "_same_device", lambda primary, target: False)
    mirror = Mirror.from_config(
        default_config(vault, backup_path=str(mirror_dir)), vault)
    assert mirror.initialize(vault) == ""
    assert (mirror_dir / MIRROR_MARKER).is_file()
    return mirror


def test_mirror_replicates_every_object_verified(tmp_path, monkeypatch):
    dump = _dump(tmp_path)
    mirror_dir = tmp_path / "mirror"
    vault = _init(tmp_path, backup=str(mirror_dir))
    cfg = default_config(vault, backup_path=str(mirror_dir))
    _initialize_mirror(vault, mirror_dir, monkeypatch)

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


def test_mirror_catches_up_after_the_fact(tmp_path, monkeypatch):
    dump = _dump(tmp_path)
    vault = _init(tmp_path)                        # scanned with NO mirror first
    pipeline.run(dump, vault, default_config(vault), dry_run=False)

    mirror_dir = tmp_path / "later-mirror"
    mirror = _initialize_mirror(vault, mirror_dir, monkeypatch)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]
    st = mirror.sync(vault, objs)
    assert st.replicated == 3 and st.failed == 0
    assert _object_names(mirror_dir) == _object_names(vault)
    # a second sync is idempotent — everything already present, nothing re-copied
    st2 = mirror.sync(vault, objs)
    assert st2.replicated == 0 and st2.already_present == 3


def test_mirror_verify_detects_a_corrupt_copy(tmp_path, monkeypatch):
    dump = _dump(tmp_path)
    mirror_dir = tmp_path / "mirror"
    vault = _init(tmp_path, backup=str(mirror_dir))
    cfg = default_config(vault, backup_path=str(mirror_dir))
    _initialize_mirror(vault, mirror_dir, monkeypatch)
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


def test_cli_mirror_init_bootstraps_the_configured_target(tmp_path, monkeypatch):
    mirror_dir = tmp_path / "mirror"
    vault = _init(tmp_path, backup=str(mirror_dir))
    monkeypatch.setattr(mirror_mod, "_same_device", lambda primary, target: False)

    assert main(["mirror", "--vault", str(vault), "--init", "--json"]) == 0
    assert (mirror_dir / MIRROR_MARKER).is_file()
    mirror = Mirror.from_config(
        default_config(vault, backup_path=str(mirror_dir)), vault)
    assert mirror.availability_problem(vault) == ""


def test_mirror_init_still_rejects_same_device(tmp_path, monkeypatch):
    vault = _init(tmp_path)
    mirror_dir = tmp_path / "same-device-mirror"
    mirror_dir.mkdir()
    monkeypatch.setattr(mirror_mod, "_same_device", lambda primary, target: True)
    mirror = Mirror.from_config(
        default_config(vault, backup_path=str(mirror_dir)), vault)

    assert "same device" in mirror.initialize(vault)
    assert not (mirror_dir / MIRROR_MARKER).exists()


def test_mirror_rejects_uninitialized_stale_mountpoint(tmp_path, monkeypatch):
    dump = _dump(tmp_path)
    vault = _init(tmp_path)
    pipeline.run(dump, vault, default_config(vault), dry_run=False)
    mirror_dir = tmp_path / "stale-mount"
    mirror_dir.mkdir()  # an ordinary local directory left behind after unmount
    monkeypatch.setattr(mirror_mod, "_same_device", lambda primary, target: False)
    mirror = Mirror.from_config(
        default_config(vault, backup_path=str(mirror_dir)), vault)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]

    st = mirror.sync(vault, objs)
    assert st.replicated == 0 and st.failed == len(objs)
    assert all("marker missing" in problem for problem in st.problems)
    assert not (mirror_dir / "objects").exists()

    empty = mirror.sync(vault, [])
    assert empty.failed == 1 and "marker missing" in empty.problems[0]


def test_mirror_rejects_objects_symlink_back_into_primary(tmp_path, monkeypatch):
    dump = _dump(tmp_path)
    vault = _init(tmp_path)
    pipeline.run(dump, vault, default_config(vault), dry_run=False)
    mirror_dir = tmp_path / "mirror"
    mirror = _initialize_mirror(vault, mirror_dir, monkeypatch)
    (mirror_dir / "objects").symlink_to(vault / "objects", target_is_directory=True)
    before = _object_names(vault)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]

    st = mirror.sync(vault, objs)
    assert st.replicated == 0 and st.failed == len(objs)
    assert all("objects/ is a symlink" in problem for problem in st.problems)
    assert _object_names(vault) == before


def test_mirror_catch_up_discovers_object_missing_from_database(tmp_path, monkeypatch):
    vault = _init(tmp_path)
    source = tmp_path / "orphan.bin"
    source.write_bytes(b"durable object whose transaction never committed")
    digest = hash_file(source)
    rel = f"objects/{digest[:2]}/{digest}.bin"
    orphan = vault / rel
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(source.read_bytes())

    mirror_dir = tmp_path / "mirror"
    mirror = _initialize_mirror(vault, mirror_dir, monkeypatch)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        db_objs = [(r["object_path"], r["hash"]) for r in db.objects_for_verify()]
    assert db_objs == []

    discovered = discover_vault_objects(vault, db_objs)
    assert (rel, digest) in discovered
    st = mirror.sync(vault, discovered)
    assert st.replicated == 1 and st.failed == 0
    assert hash_file(mirror_dir / rel) == digest
