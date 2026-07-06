"""Core data model: the Asset record.

An Asset is ARK's record of one source file. It carries the parametric metadata
(location / date-time / type / content / arbitrary custom fields) that rules
evaluate over. ``custom`` is the open-ended bag that makes the model parametric:
"add a new parameter" == put a key in ``custom``; rules can use it immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


@dataclass
class GeoPlace:
    """Resolved place for a lat/lon, from the offline reverse geocoder."""
    lat: float
    lon: float
    city: Optional[str] = None
    admin: Optional[str] = None      # state / province / region
    country: Optional[str] = None
    country_code: Optional[str] = None
    distance_km: Optional[float] = None  # to the nearest known city


@dataclass
class Asset:
    # identity / storage
    source_path: str                     # absolute path of the original (never mutated)
    hash: str                            # content hash (blake2b hex) — the vault identity
    size: int
    ext: str                             # lowercase, no dot
    kind: str                            # KIND_* coarse type
    mime: str

    # timestamps (filesystem)
    fs_created: Optional[datetime] = None
    fs_modified: Optional[datetime] = None

    # extracted / enriched metadata
    taken_at: Optional[datetime] = None  # EXIF DateTimeOriginal, tz-aware when possible
    taken_at_source: Optional[str] = None  # "exif" | "filename" | "fs" | None
    lat: Optional[float] = None
    lon: Optional[float] = None
    place: Optional[GeoPlace] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # perceptual signals (images only; None when not computable)
    phash: Optional[str] = None          # dHash fingerprint for near-duplicate detection
    blur: Optional[float] = None         # Laplacian variance; low == likely blurry

    # parametric extension: arbitrary user/derived fields (invoice vendor, project, ...)
    custom: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    # organization / lifecycle
    organized_path: Optional[str] = None   # rule-assigned path (relative to organized/)
    matched_rule: Optional[str] = None     # name of the rule that placed it
    status: Optional[str] = None
    needs_review_reasons: list[str] = field(default_factory=list)

    # db bookkeeping
    id: Optional[int] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    # ----- convenience views for the rule engine ---------------------------
    def rule_context(self) -> dict[str, Any]:
        """Flat namespace the rule DSL evaluates against.

        Exposes dotted helpers like ``place.city`` and ``taken_at.year`` plus
        every key in ``custom`` at the top level.
        """
        ctx: dict[str, Any] = {
            "ext": self.ext,
            "kind": self.kind,
            "type": self.kind,           # friendly alias used in docs/examples
            "mime": self.mime,
            "size": self.size,
            "filename": self.source_path.rsplit("/", 1)[-1],
            "camera_make": self.camera_make,
            "camera_model": self.camera_model,
            "width": self.width,
            "height": self.height,
            "blur": self.blur,           # power users can route blurry shots by rule
            "lat": self.lat,
            "lon": self.lon,
            "tags": list(self.tags),
            "place": _Namespace(
                city=self.place.city if self.place else None,
                admin=self.place.admin if self.place else None,
                country=self.place.country if self.place else None,
                country_code=self.place.country_code if self.place else None,
            ),
            "taken_at": _DateNamespace(self.taken_at),
            "date_source": self.taken_at_source,
            "has_location": self.lat is not None and self.lon is not None,
            "has_date": self.taken_at is not None,
            # a date we actually trust for confident organization (not a copy-date guess)
            "has_reliable_date": self.taken_at is not None
            and self.taken_at_source in ("exif", "filename"),
        }
        # custom fields are first-class in rules
        for k, v in self.custom.items():
            ctx.setdefault(k, v)
        return ctx

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("place", None)
        d.pop("id", None)
        return d


class _Namespace:
    """Attribute-access wrapper; unknown attrs resolve to None (never raise)."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, _name: str) -> None:  # only hit for missing keys
        return None

    def __repr__(self) -> str:
        return f"Namespace({self.__dict__})"


class _DateNamespace:
    """Exposes .year/.month/.day/.hour/.weekday/.iso for a possibly-None datetime."""

    def __init__(self, dt: Optional[datetime]) -> None:
        self._dt = dt

    @property
    def year(self) -> Optional[int]:
        return self._dt.year if self._dt else None

    @property
    def month(self) -> Optional[int]:
        return self._dt.month if self._dt else None

    @property
    def day(self) -> Optional[int]:
        return self._dt.day if self._dt else None

    @property
    def hour(self) -> Optional[int]:
        return self._dt.hour if self._dt else None

    @property
    def weekday(self) -> Optional[int]:
        return self._dt.weekday() if self._dt else None

    @property
    def iso(self) -> Optional[str]:
        return self._dt.isoformat() if self._dt else None

    def __bool__(self) -> bool:
        return self._dt is not None
