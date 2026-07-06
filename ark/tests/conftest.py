import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

# Make the `ark` package importable without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TOOLS = Path(__file__).resolve().parent.parent / "tools" / "make_sample_dump.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_sample_dump", _TOOLS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scanned(tmp_path):
    """Generate the realistic sample dump, init a vault, and commit one scan.

    Returns ``(dump, vault, stats)`` — the shared end-to-end starting point for
    the perceptual, similarity and quarantine tests."""
    from ark import pipeline
    from ark.config import load_config
    from ark.cli import cmd_init

    gen = _load_generator()
    dump = tmp_path / "dump"
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
