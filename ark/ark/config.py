"""Configuration: the vault location, backup target, and the parametric rules.

Config is TOML (`ark.toml` at the vault root). The backup target is deliberately
a small pluggable descriptor so that today's ``local`` target can become an
external drive / NAS / cloud bucket later *without touching the pipeline*.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import CONFIG_FILENAME
from .watch import WatchConfig


@dataclass
class RuleSpec:
    name: str
    when: str                      # DSL expression over asset fields (see ruleeval)
    to: str                        # destination path template, e.g. "Trips/{place.city}-{taken_at.year}"
    tags: list[str] = field(default_factory=list)


@dataclass
class BackupTarget:
    """Where the vault physically lives. `kind` selects the storage adapter.

    v1 ships `local` only; `external`/`nas`/`cloud` are recognized so config and
    the Human-Review handoff are forward-compatible.
    """
    kind: str = "local"            # local | external | nas | cloud
    path: str = ""                 # filesystem path for local/external/nas
    options: dict[str, Any] = field(default_factory=dict)  # bucket, creds ref, etc.


@dataclass
class Config:
    vault: Path
    backup: BackupTarget
    rules: list[RuleSpec]
    # organize/ tree fallback when no rule matches (still fully organized + searchable)
    fallback_to: str = "Unsorted/{kind}/{taken_at.year}/{taken_at.month:02}"
    review_to: str = "_NeedsReview/{kind}"
    # link strategy for the organized/ tree: hardlink (no data dup) or symlink or copy
    link_mode: str = "hardlink"    # hardlink | symlink | copy
    # perceptual thresholds (mirror ark.perceptual defaults; kept here so they're
    # config-driven without importing Pillow at config-load time)
    near_dup_distance: int = 10    # dHash Hamming <= this == "the same picture"
    blur_threshold: float = 60.0   # Laplacian variance < this == "likely blurry"
    # auto-ingest-on-connect settings (see ark.watch)
    watch: WatchConfig = field(default_factory=WatchConfig)

    @property
    def raw_rules(self) -> list[RuleSpec]:
        return self.rules


DEFAULT_RULES = [
    RuleSpec(
        # "photos in Goa in winter" — match the whole Goa region, not one city centroid.
        name="goa-winter-trips",
        when='kind == "image" and place.admin == "Goa" and taken_at.month in (11, 12, 1)',
        to="Trips/Goa-{taken_at.year}",
        tags=["trip", "goa"],
    ),
    RuleSpec(
        name="photos-by-place",
        when='kind == "image" and has_location and has_reliable_date',
        to="Photos/{place.country}/{place.city}/{taken_at.year}",
        tags=["photo"],
    ),
    RuleSpec(
        name="photos-by-date",
        when='kind == "image" and has_reliable_date',
        to="Photos/{taken_at.year}/{taken_at.month:02}",
        tags=["photo"],
    ),
    RuleSpec(
        # Set a custom `vendor` field (P2+ extraction) and this template picks it up.
        name="invoices",
        when='kind == "document" and "invoice" in filename.lower()',
        to="Documents/Invoices/{taken_at.year}",
        tags=["invoice"],
    ),
    RuleSpec(
        name="documents",
        when='kind == "document" and has_reliable_date',
        to="Documents/{taken_at.year}",
        tags=["document"],
    ),
    RuleSpec(
        name="videos",
        when='kind == "video" and has_reliable_date',
        to="Videos/{taken_at.year}/{taken_at.month:02}",
        tags=["video"],
    ),
]


def default_config(vault: Path, backup_path: str | None = None) -> Config:
    vault = Path(vault).expanduser().resolve()
    return Config(
        vault=vault,
        backup=BackupTarget(kind="local", path=backup_path or str(vault)),
        rules=list(DEFAULT_RULES),
    )


def load_config(vault: Path) -> Config:
    """Load `ark.toml` from the vault's `.ark` dir, falling back to defaults."""
    from .constants import DIR_META

    vault = Path(vault).expanduser().resolve()
    cfg_path = vault / DIR_META / CONFIG_FILENAME
    if not cfg_path.exists():
        return default_config(vault)

    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    rules = [
        RuleSpec(
            name=r["name"],
            when=r["when"],
            to=r["to"],
            tags=list(r.get("tags", [])),
        )
        for r in data.get("rules", [])
    ] or list(DEFAULT_RULES)

    bt = data.get("backup", {})
    backup = BackupTarget(
        kind=bt.get("kind", "local"),
        path=bt.get("path", str(vault)),
        options=bt.get("options", {}),
    )
    opts = data.get("options", {})
    return Config(
        vault=vault,
        backup=backup,
        rules=rules,
        fallback_to=opts.get("fallback_to", Config.fallback_to),
        review_to=opts.get("review_to", Config.review_to),
        link_mode=opts.get("link_mode", Config.link_mode),
        near_dup_distance=int(opts.get("near_dup_distance", Config.near_dup_distance)),
        blur_threshold=float(opts.get("blur_threshold", Config.blur_threshold)),
        watch=_load_watch(data.get("watch", {})),
    )


def _load_watch(w: dict) -> WatchConfig:
    d = WatchConfig()
    return WatchConfig(
        enabled=bool(w.get("enabled", d.enabled)),
        interval=float(w.get("interval", d.interval)),
        debounce=float(w.get("debounce", d.debounce)),
        mount_root=str(w.get("mount_root", d.mount_root)),
        sources=tuple(w.get("sources", d.sources)),
        ignore=tuple(w.get("ignore", d.ignore)),
        subpath=str(w.get("subpath", d.subpath)),
    )


def render_config_toml(cfg: Config) -> str:
    """Serialize a Config back to TOML (stdlib has no writer, so we format it)."""
    lines: list[str] = [
        "# ARK vault configuration.",
        "# Non-destructive by default: ARK only reads sources and copies into the vault.",
        "",
        "[backup]",
        f'kind = "{cfg.backup.kind}"   # local | external | nas | cloud',
        "# Off-site MIRROR target — set to an external SSD/NAS (different from the vault)",
        "# and every object is also replicated + verified there. Equal to the vault = no mirror.",
        "# After setting a target, connect it and run `ark mirror --init` once before syncing.",
        f'path = {_toml_str(cfg.backup.path)}',
        "",
        "[options]",
        f'link_mode = "{cfg.link_mode}"   # hardlink | symlink | copy',
        f'fallback_to = {_toml_str(cfg.fallback_to)}',
        f'review_to = {_toml_str(cfg.review_to)}',
        f'near_dup_distance = {cfg.near_dup_distance}   # perceptual Hamming distance for near-duplicates',
        f'blur_threshold = {cfg.blur_threshold}   # Laplacian variance below == likely blurry',
        "",
        "# Auto-ingest on connect (ark watch): scan a volume the moment it mounts.",
        "# Non-destructive like every scan; a reconnected card is re-scanned (dedup makes it cheap).",
        "[watch]",
        f'enabled = {str(cfg.watch.enabled).lower()}',
        f'interval = {cfg.watch.interval}   # seconds between mount-root polls',
        f'debounce = {cfg.watch.debounce}   # settle time after a volume appears',
        f'mount_root = {_toml_str(cfg.watch.mount_root)}   # macOS: /Volumes',
        "# Explicit allowlist; empty means auto-ingest nothing. Prefer exact names and",
        "# OS-level device/volume UUID pinning when your mount setup supports it: names are mutable.",
        f'sources = [{", ".join(_toml_str(s) for s in cfg.watch.sources)}]   # volume-name globs to ingest',
        f'ignore = [{", ".join(_toml_str(s) for s in cfg.watch.ignore)}]',
        f'subpath = {_toml_str(cfg.watch.subpath)}   # optional subdir to scan, e.g. "DCIM"',
        "",
        "# Parametric organization rules — evaluated top-to-bottom; first match wins.",
        "# `when` is a safe expression over asset fields (kind, place.city, taken_at.year, ...).",
        "# `to` is a path template; {place.city}, {taken_at.year:04}, {vendor} etc.",
    ]
    for r in cfg.rules:
        lines += [
            "",
            "[[rules]]",
            f'name = {_toml_str(r.name)}',
            f'when = {_toml_str(r.when)}',
            f'to = {_toml_str(r.to)}',
            f"tags = [{', '.join(_toml_str(t) for t in r.tags)}]",
        ]
    return "\n".join(lines) + "\n"


def _toml_str(s: str) -> str:
    # Prefer single-quote literal strings; fall back to escaped basic strings.
    if "'" not in s:
        return f"'{s}'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
