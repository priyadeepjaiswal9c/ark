"""The sacred rule under test: ARK never mutates a source, never writes outside
the vault, dedups by content, and verifies every copy."""

import os
from pathlib import Path

import pytest

from ark.backup import VaultWriter, VaultError
from ark.config import default_config
from ark.hashing import hash_file
from ark.models import Asset
from ark.rules import RuleMatch


def _asset(path: Path) -> Asset:
    return Asset(
        source_path=str(path), hash=hash_file(path), size=path.stat().st_size,
        ext=path.suffix.lstrip("."), kind="image", mime="image/jpeg",
    )


def _writer(tmp_path: Path) -> VaultWriter:
    vault = tmp_path / "vault"
    (vault).mkdir()
    return VaultWriter(vault, default_config(vault))


def test_source_is_never_mutated(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"original-bytes" * 100)
    before = hash_file(src)
    before_mtime = src.stat().st_mtime

    w = _writer(tmp_path)
    a = _asset(src)
    res = w.store(a, RuleMatch("r", "Photos/2025"))
    assert res.action == "stored"
    assert res.verified
    assert src.exists()
    assert hash_file(src) == before          # bytes identical
    assert src.stat().st_mtime == before_mtime  # not even touched


def test_dedup_stores_once(tmp_path):
    a1 = tmp_path / "a.jpg"; a1.write_bytes(b"same" * 500)
    a2 = tmp_path / "b.jpg"; a2.write_bytes(b"same" * 500)  # identical content
    w = _writer(tmp_path)
    r1 = w.store(_asset(a1), RuleMatch("r", "X"))
    r2 = w.store(_asset(a2), RuleMatch("r", "Y"))
    assert r1.is_new_object is True
    assert r2.is_new_object is False          # same bytes -> not re-stored
    assert r1.object_relpath == r2.object_relpath
    # exactly one object file on disk
    objs = list((w.objects).rglob("*"))
    assert len([o for o in objs if o.is_file()]) == 1


def test_path_traversal_is_blocked(tmp_path):
    src = tmp_path / "evil.jpg"; src.write_bytes(b"x" * 10)
    w = _writer(tmp_path)
    # a destination trying to climb out of the vault must be refused
    with pytest.raises(VaultError):
        w.store(_asset(src), RuleMatch("r", "../../../../tmp/pwned"))


def test_collision_does_not_overwrite(tmp_path):
    # Two DIFFERENT files sharing a basename must both survive under one dir.
    d1 = tmp_path / "dir1"; d1.mkdir()
    d2 = tmp_path / "dir2"; d2.mkdir()
    a1 = d1 / "IMG.jpg"; a1.write_bytes(b"content-one" * 50)
    a2 = d2 / "IMG.jpg"; a2.write_bytes(b"content-two" * 50)  # same name, other bytes
    w = _writer(tmp_path)
    m = RuleMatch("r", "Photos")
    r1 = w.store(_asset(a1), m)
    r2 = w.store(_asset(a2), m)

    dst_dir = w.organized / "Photos"
    files = sorted(p.name for p in dst_dir.iterdir())
    assert len(files) == 2                       # neither clobbered
    assert r1.organized_relpath != r2.organized_relpath
    # both objects exist and each organized entry maps to the right content
    assert (w.vault / r1.organized_relpath).exists()
    assert (w.vault / r2.organized_relpath).exists()
    assert hash_file(w.vault / r1.organized_relpath) != hash_file(w.vault / r2.organized_relpath)


def test_corrupt_copy_fails_soft(tmp_path, monkeypatch):
    src = tmp_path / "p.jpg"; src.write_bytes(b"good" * 100)
    w = _writer(tmp_path)
    # force verify to fail as if the copy were corrupted
    monkeypatch.setattr("ark.backup.verify_copy", lambda *a, **k: (False, ""))
    res = w.store(_asset(src), RuleMatch("r", "Photos"))
    assert res.action == "failed"
    assert src.exists() and hash_file(src)  # source still intact
    # no half-written object left behind
    leftovers = [p for p in w.objects.rglob("*") if p.is_file()]
    assert leftovers == []


def test_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "p.jpg"; src.write_bytes(b"data" * 100)
    w = _writer(tmp_path)
    res = w.store(_asset(src), RuleMatch("r", "Photos"), dry_run=True)
    assert res.action == "stored"
    assert not any(w.objects.rglob("*"))
    assert not (w.organized).exists() or not any(w.organized.rglob("*"))


def test_verify_vault_detects_corruption(tmp_path):
    src = tmp_path / "p.jpg"; src.write_bytes(b"data" * 100)
    w = _writer(tmp_path)
    res = w.store(_asset(src), RuleMatch("r", "Photos"))
    assert w.verify_vault() == []
    # corrupt an object on disk
    obj = w.vault / res.object_relpath
    obj.write_bytes(b"tampered")
    problems = w.verify_vault()
    assert any(p["path"] == res.object_relpath for p in problems)
