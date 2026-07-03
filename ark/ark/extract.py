"""Metadata extraction — read-only, industrial-grade, fails soft.

Pulls filesystem facts (size, timestamps), the true type, and — for images —
EXIF date/time (timezone-aware when the file records an offset), GPS, camera,
and dimensions. Every parser is defensive: a corrupt or metadata-less file never
raises, it just yields fewer fields (and later lands in the "needs review"
bucket rather than being dropped or guessed at).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import piexif
from PIL import Image

from . import filetype
from .constants import KIND_IMAGE
from .hashing import hash_file
from .models import Asset

# IMG_20251215_143022 / 20251215_143022 / 2025-12-15 14.30.22 / VID_20251215
_FN_DT = re.compile(
    r"(?P<y>20\d{2})[-_.]?(?P<mo>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[-_ tT.]?(?P<h>[01]\d|2[0-3])[-_.:]?(?P<mi>[0-5]\d)(?:[-_.:]?(?P<s>[0-5]\d))?)?"
)


def extract(path: str | Path) -> Asset:
    p = Path(path)
    st = p.stat()
    ext, kind, mime = filetype.detect(p)

    asset = Asset(
        source_path=str(p.resolve()),
        hash=hash_file(p),
        size=st.st_size,
        ext=ext,
        kind=kind,
        mime=mime,
        fs_modified=_dt_from_ts(st.st_mtime),
        fs_created=_dt_from_ts(getattr(st, "st_birthtime", st.st_ctime)),
    )

    if kind == KIND_IMAGE:
        _extract_image(p, asset)

    # Date fallback chain: EXIF → filename → filesystem.
    if asset.taken_at is None:
        fn_dt = _date_from_filename(p.name)
        if fn_dt is not None:
            asset.taken_at, asset.taken_at_source = fn_dt, "filename"
    if asset.taken_at is None and asset.fs_created is not None:
        asset.taken_at, asset.taken_at_source = asset.fs_created, "fs"

    return asset


def _extract_image(p: Path, asset: Asset) -> None:
    # Dimensions (works for jpg/png/gif/webp/bmp/tiff; HEIC needs a plugin — skip soft).
    try:
        with Image.open(p) as im:
            asset.width, asset.height = im.width, im.height
    except Exception:
        pass

    # EXIF (JPEG/TIFF). Anything else simply has none.
    try:
        exif = piexif.load(str(p))
    except Exception:
        return

    zeroth, exif_ifd, gps_ifd = exif.get("0th", {}), exif.get("Exif", {}), exif.get("GPS", {})

    asset.camera_make = _clean(zeroth.get(piexif.ImageIFD.Make))
    asset.camera_model = _clean(zeroth.get(piexif.ImageIFD.Model))

    dt_raw = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal) or zeroth.get(piexif.ImageIFD.DateTime)
    offset = _clean(exif_ifd.get(piexif.ExifIFD.OffsetTimeOriginal))
    taken = _parse_exif_dt(dt_raw, offset)
    if taken is not None:
        asset.taken_at, asset.taken_at_source = taken, "exif"

    latlon = _parse_gps(gps_ifd)
    if latlon is not None:
        asset.lat, asset.lon = latlon


# ---- helpers ---------------------------------------------------------------

def _dt_from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    v = v.strip().strip("\x00").strip()
    return v or None


def _parse_exif_dt(raw, offset: Optional[str]) -> Optional[datetime]:
    s = _clean(raw)
    if not s:
        return None
    # EXIF canonical form: "YYYY:MM:DD HH:MM:SS"
    try:
        dt = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        m = _FN_DT.search(s)
        if not m:
            return None
        dt = _dt_from_match(m)
        if dt is None:
            return None
    tz = _parse_offset(offset)
    return dt.replace(tzinfo=tz) if tz is not None else dt


def _parse_offset(offset: Optional[str]) -> Optional[timezone]:
    if not offset or len(offset) < 3:
        return None
    try:
        sign = 1 if offset[0] == "+" else -1
        hh, mm = offset[1:].split(":")
        return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
    except (ValueError, IndexError):
        return None


def _date_from_filename(name: str) -> Optional[datetime]:
    m = _FN_DT.search(name)
    return _dt_from_match(m) if m else None


def _dt_from_match(m: re.Match) -> Optional[datetime]:
    try:
        return datetime(
            int(m["y"]), int(m["mo"]), int(m["d"]),
            int(m["h"] or 0), int(m["mi"] or 0), int(m["s"] or 0),
        )
    except (ValueError, TypeError):
        return None


def _parse_gps(gps: dict) -> Optional[tuple[float, float]]:
    try:
        lat = _dms_to_deg(gps[piexif.GPSIFD.GPSLatitude], gps[piexif.GPSIFD.GPSLatitudeRef])
        lon = _dms_to_deg(gps[piexif.GPSIFD.GPSLongitude], gps[piexif.GPSIFD.GPSLongitudeRef])
    except (KeyError, TypeError, ZeroDivisionError, ValueError):
        return None
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _dms_to_deg(dms, ref) -> Optional[float]:
    if not dms or len(dms) != 3:
        return None
    def r(x):
        n, d = x
        return n / d if d else 0.0
    deg = r(dms[0]) + r(dms[1]) / 60.0 + r(dms[2]) / 3600.0
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "replace")
    # EXIF ASCII fields are NUL-terminated, so refs commonly arrive as "S\x00"/
    # "W\x00". Strip that (and whitespace) before the hemisphere check, or every
    # southern/western photo silently flips to the wrong hemisphere.
    ref = (ref or "").strip().strip("\x00").strip().upper()[:1]
    if ref in ("S", "W"):
        deg = -deg
    return deg
