"""End-to-end: generate the realistic sample dump, scan it, and assert the whole
vault-core story — organize, dedup, versioned safe backup, search, cleanup."""

import importlib.util
from pathlib import Path

import pytest

from ark import pipeline, search as search_mod, report
from ark.config import default_config, load_config
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database
from ark.cli import cmd_init
import argparse

TOOLS = Path(__file__).resolve().parent.parent / "tools" / "make_sample_dump.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_sample_dump", TOOLS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scanned(tmp_path):
    gen = _load_generator()
    dump = tmp_path / "dump"
    import sys
    old = sys.argv
    sys.argv = ["make_sample_dump.py", str(dump)]
    try:
        gen.main()
    finally:
        sys.argv = old

    vault = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(vault), backup=None, json=True))
    cfg = load_config(vault)
    stats = pipeline.run(dump, vault, cfg, dry_run=False)
    return dump, vault, stats


def test_counts(scanned):
    _, _, stats = scanned
    c = stats.as_counts()
    assert c["total"] == 12
    assert c["duplicates"] == 1          # copy_of_goa.jpg
    assert c["failed"] == 0
    assert c["by_kind"]["image"] == 9


def test_goa_trip_grouped(scanned):
    _, vault, _ = scanned
    trip = vault / "organized" / "Trips" / "Goa-2025"
    names = sorted(p.name for p in trip.iterdir())
    # all three December Goa-region photos + the duplicate copy
    assert len(names) == 4
    assert "IMG_20251224_101500.jpg" in names


def test_dedup_single_object(scanned):
    _, vault, stats = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        s = db.stats()
    assert s["assets"] == 12
    assert s["objects"] == 11             # one pair deduped
    assert s["duplicates"] == 1


def test_nl_search_finds_pdf(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        hits = search_mod.search(db, "the PDF from the Goa trip")
        assert any(h.organized_path and h.organized_path.endswith(".pdf") for h in hits)
        # filter search: images in the Goa region
        goa = search_mod.search(db, "kind:image place:Goa")
        assert len(goa) >= 3


def test_cleanup_report_is_safe_and_proven(scanned):
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = report.cleanup_report(db, vault)
    # every backed-up, verified source is clearable; the exact copy is flagged
    assert len(rep.safe_to_delete) == 12
    assert rep.at_risk == []
    assert rep.exact_duplicates == 1               # 2 identical files == 1 redundant EXTRA
    dup_names = {Path(c.source_path).name for c in rep.safe_to_delete if c.is_exact_duplicate}
    assert "copy_of_goa.jpg" in dup_names
    for c in rep.safe_to_delete:                    # every proof object exists
        assert (vault / c.canonical_object).exists()


def test_cleanup_flags_corrupt_object_as_at_risk_not_safe(scanned):
    """The critical guarantee: if the vault copy is corrupt, the sources whose
    only backup is that object must NOT be reported safe to delete."""
    _, vault, _ = scanned
    with Database(vault / DIR_META / DB_FILENAME) as db:
        rep = report.cleanup_report(db, vault)
        # corrupt a single-copy object (a unique photo) after backup
        target = next(c for c in rep.safe_to_delete if not c.is_exact_duplicate)
        (vault / target.canonical_object).write_bytes(b"corrupted-now")
        rep2 = report.cleanup_report(db, vault)
    safe_paths = {c.source_path for c in rep2.safe_to_delete}
    risk_paths = {a.source_path for a in rep2.at_risk}
    assert target.source_path not in safe_paths     # no longer "safe"
    assert target.source_path in risk_paths
    assert rep2.at_risk[0].problem == "vault object corrupt"


def test_rescan_is_idempotent(scanned):
    dump, vault, _ = scanned
    cfg = load_config(vault)
    stats2 = pipeline.run(dump, vault, cfg, dry_run=False)
    with Database(vault / DIR_META / DB_FILENAME) as db:
        s = db.stats()
    # a second scan must not create new assets or objects
    assert s["assets"] == 12
    assert s["objects"] == 11
