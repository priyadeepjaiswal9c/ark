#!/usr/bin/env python3
"""Generate a realistic unsorted SSD-dump for building/validating ARK.

Mirrors the real Samsung → Mac → SSD dump: geotagged photos across cities and
dates, a December Goa trip (for the trip rule), an invoice PDF, plain docs, a
video, plus the messy edge cases ARK must survive — a photo with no EXIF at all,
an exact byte-for-byte duplicate, and awkward filenames.

Run:  python tools/make_sample_dump.py [target_dir]   (default: ./sample-dump)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

# (filename, subdir, w, h, color, make, model, "YYYY:MM:DD HH:MM:SS", tz, lat, lon)
PHOTOS = [
    ("IMG_20251224_101500.jpg", "", 640, 480, (30, 120, 180), "samsung", "SM-S911B",
     "2025:12:24 10:15:00", "+05:30", 15.4909, 73.8278),   # Goa, December -> Trips/Goa-2025
    ("IMG_20251226_173000.jpg", "", 640, 480, (200, 120, 40), "samsung", "SM-S911B",
     "2025:12:26 17:30:00", "+05:30", 15.2993, 74.1240),   # Goa, December
    ("IMG_20251227_090000.jpg", "DCIM", 640, 480, (60, 160, 90), "samsung", "SM-S911B",
     "2025:12:27 09:00:00", "+05:30", 15.2832, 73.9862),   # Margao (Goa) December
    ("IMG_20250815_120000.jpg", "DCIM", 800, 600, (150, 40, 160), "samsung", "SM-S911B",
     "2025:08:15 12:00:00", "+05:30", 28.6139, 77.2090),   # New Delhi, August
    ("IMG_20250704_183000.jpg", "", 800, 600, (220, 180, 30), "Google", "Pixel 8 Pro",
     "2025:07:04 18:30:00", "-07:00", 37.7749, -122.4194), # San Francisco, July
    ("IMG_20240922_140000.jpg", "trip", 800, 600, (40, 60, 200), "Apple", "iPhone 15",
     "2024:09:22 14:00:00", "+01:00", 51.5074, -0.1278),   # London 2024
    ("IMG_20250301_080000.jpg", "trip", 800, 600, (200, 60, 60), "Google", "Pixel 8 Pro",
     "2025:03:01 08:00:00", "+09:00", 35.6762, 139.6503),  # Tokyo March
]


def _dms(deg: float):
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600, 4)
    return ((d, 1), (m, 1), (int(s * 100), 100))


def _make_jpeg(path: Path, w, h, color, make, model, dt, tz, lat, lon) -> None:
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    d.rectangle([w * 0.1, h * 0.1, w * 0.9, h * 0.9], outline=(255, 255, 255), width=3)
    d.text((w * 0.15, h * 0.15), path.stem, fill=(255, 255, 255))

    zeroth = {piexif.ImageIFD.Make: make.encode(), piexif.ImageIFD.Model: model.encode()}
    exif = {
        piexif.ExifIFD.DateTimeOriginal: dt.encode(),
        piexif.ExifIFD.OffsetTimeOriginal: tz.encode(),
    }
    gps = {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _dms(lon),
    }
    exif_bytes = piexif.dump({"0th": zeroth, "Exif": exif, "GPS": gps, "1st": {}, "thumbnail": None})
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "jpeg", exif=exif_bytes, quality=85)


_MIN_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \ntrailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n164\n%%EOF\n"
)
# minimal MP4: ftyp box so the type sniffer sees a video
_MIN_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    + b"\x00\x00\x00\x08free" + b"\x00" * 64
)


def main() -> None:
    target = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 \
        else Path.cwd() / "sample-dump"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    n = 0
    for spec in PHOTOS:
        name, sub = spec[0], spec[1]
        _make_jpeg(target / sub / name, *spec[2:])
        n += 1

    # exact duplicate (same bytes, different name + folder) -> dedup + cleanup intel
    shutil.copy2(target / "IMG_20251224_101500.jpg", target / "DCIM" / "copy_of_goa.jpg")
    n += 1

    # photo with NO EXIF at all -> needs review (no date, no place)
    Image.new("RGB", (300, 200), (90, 90, 90)).save(target / "screenshot_no_exif.png")
    n += 1

    # invoice PDF (filename triggers the invoices rule) + a plain doc
    (target / "docs").mkdir(exist_ok=True)
    (target / "docs" / "invoice_acme_2025.pdf").write_bytes(_MIN_PDF)
    (target / "docs" / "notes_goa_trip.txt").write_text(
        "Goa trip Dec 2025 — beaches, the PDF invoice from Acme is in this folder.\n",
        encoding="utf-8")
    n += 2

    # a video with a date in the filename
    (target / "VID_20251225_200000.mp4").write_bytes(_MIN_MP4)
    n += 1

    # OS cruft that must be ignored
    (target / ".DS_Store").write_bytes(b"\x00\x01cruft")
    (target / "DCIM" / "._IMG_20251224_101500.jpg").write_bytes(b"applereserved")

    print(f"wrote {n} real files (+cruft to ignore) -> {target}")
    print("tree:")
    for p in sorted(target.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(target)}  ({p.stat().st_size} B)")


if __name__ == "__main__":
    main()
