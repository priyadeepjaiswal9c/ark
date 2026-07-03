"""Regression tests for the defects found in the adversarial safety review."""

from pathlib import Path

import pytest

from ark import ruleeval, rules as rules_mod, pipeline
from ark.backup import VaultWriter
from ark.config import default_config
from ark.extract import _dms_to_deg, _parse_gps
from ark.hashing import hash_file
from ark.models import Asset
from ark.rules import RuleMatch, validate_rules, _render


def _asset(path: Path, kind="image") -> Asset:
    return Asset(source_path=str(path), hash=hash_file(path), size=path.stat().st_size,
                 ext=path.suffix.lstrip("."), kind=kind, mime="application/octet-stream")


def _writer(tmp_path):
    v = tmp_path / "vault"; v.mkdir()
    return VaultWriter(v, default_config(v))


# ---- CRITICAL #1: existing object re-verified; corruption self-heals --------

def test_corrupt_existing_object_is_healed_not_trusted(tmp_path):
    src = tmp_path / "p.jpg"; src.write_bytes(b"the-only-good-copy" * 100)
    w = _writer(tmp_path)
    r1 = w.store(_asset(src), RuleMatch("r", "Photos"))
    assert r1.action == "stored"

    # corrupt the stored object, then re-store the SAME source
    (w.vault / r1.object_relpath).write_bytes(b"corrupt")
    r2 = w.store(_asset(src), RuleMatch("r", "Photos"))

    assert r2.action == "healed"                 # NOT silently "duplicate"
    assert r2.vault_has_good_copy is False        # source is NOT safe to delete
    # object now re-hashes to its correct content address
    assert hash_file(w.vault / r2.object_relpath) == r2.hash
    assert w.verify_vault() == []


def test_valid_existing_object_is_a_real_duplicate(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"same-bytes" * 100)
    b = tmp_path / "b.jpg"; b.write_bytes(b"same-bytes" * 100)
    w = _writer(tmp_path)
    w.store(_asset(a), RuleMatch("r", "X"))
    r2 = w.store(_asset(b), RuleMatch("r", "Y"))
    assert r2.action == "duplicate"
    assert r2.vault_has_good_copy is True         # b really is redundant + safe


# ---- HIGH #3: one failing file must not abort the scan ---------------------

def test_scan_survives_a_file_whose_rule_raises(tmp_path, monkeypatch):
    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "good.txt").write_text("hello")
    (dump / "bad.txt").write_text("world")
    vault = tmp_path / "vault"
    from ark.cli import cmd_init
    import argparse
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)

    real_apply = pipeline.apply_rules
    def boom(asset, c):
        if asset.source_path.endswith("bad.txt"):
            raise RuntimeError("simulated downstream failure")
        return real_apply(asset, c)
    monkeypatch.setattr(pipeline, "apply_rules", boom)

    stats = pipeline.run(dump, vault, cfg)          # must NOT raise
    assert stats.total == 2
    assert stats.failed == 1                        # only the bad file
    assert (stats.stored + stats.needs_review + stats.duplicates) == 1


# ---- HIGH #4: GPS hemisphere ref with a trailing NUL -----------------------

def test_gps_ref_with_trailing_nul_still_flips_hemisphere():
    dms = [(12, 1), (30, 1), (0, 1)]
    assert _dms_to_deg(dms, b"S\x00") == pytest.approx(-12.5)
    assert _dms_to_deg(dms, b"W\x00") == pytest.approx(-12.5)
    assert _dms_to_deg(dms, b"N\x00") == pytest.approx(12.5)


def test_parse_gps_southern_hemisphere_with_nul():
    import piexif
    gps = {
        piexif.GPSIFD.GPSLatitudeRef: b"S\x00",
        piexif.GPSIFD.GPSLatitude: ((33, 1), (52, 1), (0, 1)),
        piexif.GPSIFD.GPSLongitudeRef: b"E\x00",
        piexif.GPSIFD.GPSLongitude: ((151, 1), (12, 1), (0, 1)),
    }
    lat, lon = _parse_gps(gps)
    assert lat < 0 and lon > 0                       # Sydney: south + east


# ---- #7 + DoS class: template width + arithmetic sandbox -------------------

def test_abusive_format_spec_rejected_and_neutralized():
    from ark.config import Config, RuleSpec, default_config
    cfg = default_config("/tmp/v")
    cfg.rules.append(RuleSpec(name="dos", when="True", to="X/{taken_at.year:>900000000}"))
    assert validate_rules(cfg)                        # flagged as invalid
    # and even if it slipped through, rendering does not allocate 900 MB
    rel, _ = _render("X/{n:>900000000}", {"n": 7})
    assert rel == "X/7"


def test_multiplicative_operators_are_out_of_the_sandbox():
    for expr in ('"x" * 1000000000', "10 * 10", "size / 0", "n % 2", "n // 2"):
        with pytest.raises(ruleeval.RuleError):
            ruleeval.validate(expr)
    # additive operators still work
    assert ruleeval.evaluate("1 + 1 == 2", {}) is True


# ---- #8: dry-run must not mutate the real DB file --------------------------

def test_dry_run_does_not_touch_the_real_db(tmp_path):
    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "a.txt").write_text("hi")
    vault = tmp_path / "vault"
    from ark.cli import cmd_init
    from ark.constants import DIR_META, DB_FILENAME
    import argparse
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    db_path = vault / DIR_META / DB_FILENAME
    before = db_path.stat().st_mtime_ns

    cfg = default_config(vault)
    pipeline.run(dump, vault, cfg, dry_run=True)

    # read-only seed => the real DB file is never modified
    assert db_path.stat().st_mtime_ns == before


# ---- #10: an unchanged rescan is 'unchanged', not 'newly stored' -----------

def test_unchanged_rescan_reports_no_new_bytes(tmp_path):
    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "a.txt").write_text("hello world")
    vault = tmp_path / "vault"
    from ark.cli import cmd_init
    import argparse
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)

    s1 = pipeline.run(dump, vault, cfg)
    assert s1.stored == 1 and s1.unchanged == 0
    s2 = pipeline.run(dump, vault, cfg)               # identical rescan
    assert s2.unchanged == 1
    assert s2.stored == 0
    assert s2.bytes_new == 0


def test_cleanup_never_calls_a_changed_source_safe(tmp_path):
    """CRITICAL: if a source file changed after its last scan, cleanup must NOT
    report it safe against the stale (old-content) vault object."""
    from ark import report
    from ark.db import Database
    from ark.constants import DIR_META, DB_FILENAME
    from ark.cli import cmd_init
    import argparse

    dump = tmp_path / "dump"; dump.mkdir()
    src = dump / "pic.txt"; src.write_text("ORIGINAL content H1")
    vault = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)
    pipeline.run(dump, vault, cfg)

    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert len(report.cleanup_report(db, vault).safe_to_delete) == 1  # backed up, safe
        # now the source's CURRENT bytes diverge from what was backed up
        src.write_text("EDITED content H2 — never backed up")
        rep = report.cleanup_report(db, vault)
    assert rep.safe_to_delete == []                 # must NOT say safe
    assert len(rep.at_risk) == 1
    assert "changed" in rep.at_risk[0].problem


def test_cleanup_excludes_needs_review_assets(tmp_path):
    from ark import report
    from ark.config import RuleSpec
    from ark.db import Database
    from ark.constants import DIR_META, DB_FILENAME
    from ark.cli import cmd_init
    import argparse

    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "note.txt").write_text("hi")
    vault = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)
    cfg.rules = [RuleSpec("needs-vendor", 'kind == "document"', "Docs/{vendor}")]  # vendor unknown
    pipeline.run(dump, vault, cfg)

    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = report.cleanup_report(db, vault)
    assert rep.needs_review == 1
    assert rep.safe_to_delete == []                 # not double-booked as "safe"


def test_rescan_over_a_corrupt_object_reports_healed_not_unchanged(tmp_path):
    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "a.txt").write_text("precious data")
    vault = tmp_path / "vault"
    from ark.cli import cmd_init
    from ark.constants import DIR_OBJECTS
    import argparse
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)

    pipeline.run(dump, vault, cfg)
    # corrupt the one stored object IN PLACE (also corrupts its organized hardlink)
    obj = next(p for p in (vault / DIR_OBJECTS).rglob("*") if p.is_file())
    from ark.constants import DIR_ORGANIZED
    org = next(p for p in (vault / DIR_ORGANIZED).rglob("*") if p.is_file())
    obj.write_bytes(b"rot")
    assert org.read_bytes() == b"rot"                # hardlink shares the corruption
    s = pipeline.run(dump, vault, cfg)               # rescan self-heals
    assert s.healed == 1 and s.unchanged == 0
    assert obj.read_bytes() == b"precious data"      # object restored
    assert org.read_bytes() == b"precious data"      # AND the organized view restored
    assert VaultWriter(vault, cfg).verify_vault() == []


def test_rescan_of_a_MISSING_object_reports_healed_not_unchanged(tmp_path):
    """A vault object deleted between runs is rebuilt on rescan — that's a heal,
    not 'nothing written'."""
    dump = tmp_path / "dump"; dump.mkdir()
    (dump / "a.txt").write_text("data")
    vault = tmp_path / "vault"
    from ark.cli import cmd_init
    from ark.constants import DIR_OBJECTS
    import argparse
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = default_config(vault)

    pipeline.run(dump, vault, cfg)
    obj = next(p for p in (vault / DIR_OBJECTS).rglob("*") if p.is_file())
    obj.unlink()                                     # vault silently loses the object
    s = pipeline.run(dump, vault, cfg)
    assert s.healed == 1 and s.unchanged == 0        # rebuild is surfaced, not hidden
    assert VaultWriter(vault, cfg).verify_vault() == []


def test_organize_failure_after_backup_keeps_the_object(tmp_path, monkeypatch):
    """If the object is written but linking into organized/ fails, the backup is
    already durable — store() must report success, not 'failed'."""
    src = tmp_path / "p.jpg"; src.write_bytes(b"content" * 100)
    w = _writer(tmp_path)
    def boom(*a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(w, "_link_into_organized", boom)
    res = w.store(_asset(src), RuleMatch("r", "Photos"))
    assert res.action == "stored"                    # NOT "failed"
    assert res.is_new_object is True                 # so the pipeline registers the good object
    assert res.error and "organizing failed" in res.error
    assert w.verify_vault() == []                    # the object is genuinely good
