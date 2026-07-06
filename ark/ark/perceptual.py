"""Perceptual analysis of images — near-duplicate and blur detection.

This is the P2 intelligence layer, kept deliberately dependency-light: it uses
only Pillow (already required for extraction) plus the standard library. No
numpy, no OpenCV, no ML weights.

Two signals, both cheap enough to compute during a normal scan:

  * **dHash** (difference hash) — a 64-bit perceptual fingerprint. Two images
    whose dHashes are within a small Hamming distance are *visually* the same
    picture even if their bytes differ (re-compressed, resized, lightly edited,
    a burst frame). The content hash (blake2b) only catches byte-identical
    copies; dHash catches the near-duplicates a human would call "the same shot".
    dHash is chosen over aHash/pHash because it is robust to brightness/gamma
    shifts and JPEG recompression without needing a DCT.

  * **blur score** — the variance of the Laplacian over a normalized grayscale
    image. Sharp images have lots of high-frequency edge energy (high variance);
    blurry / out-of-focus images have little (low variance). This is the classic
    "variance of Laplacian" focus measure, computed here in pure Python so it
    needs no native imaging kernels.

Both are *hints*, never verdicts. ARK surfaces them for review and (opt-in)
reversible quarantine; it never deletes anything on their say-so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageStat

from .constants import KIND_IMAGE

# ---- tunables (all overridable from config / CLI) --------------------------
DHASH_SIDE = 8                 # -> (DHASH_SIDE * DHASH_SIDE) = 64-bit hash
BLUR_SIDE = 128                # normalize every image to this square before scoring
DEFAULT_NEAR_DUP_DISTANCE = 10  # Hamming distance <= this  == "same picture" (of 64)
DEFAULT_BLUR_THRESHOLD = 60.0   # Laplacian variance < this == likely blurry

_HEX_WIDTH = (DHASH_SIDE * DHASH_SIDE) // 4  # hex chars in a dhash string (16 for 64-bit)


# ---- perceptual hash -------------------------------------------------------

def dhash(image: Image.Image, side: int = DHASH_SIDE) -> str:
    """Return the difference-hash of ``image`` as a zero-padded hex string.

    Row-wise gradient hash: resize to ``(side+1) x side`` grayscale, then for
    each row emit one bit per adjacent-pixel comparison. ``side=8`` -> 64 bits.
    """
    small = image.convert("L").resize((side + 1, side), Image.Resampling.LANCZOS)
    px = small.tobytes()                # row-major bytes, one per pixel, width == side + 1
    stride = side + 1
    bits = 0
    for row in range(side):
        base = row * stride
        for col in range(side):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return format(bits, "0{}x".format(side * side // 4))


def hamming(a: str, b: str) -> int:
    """Hamming distance between two hex dhash strings.

    A large distance (e.g. mismatched lengths / non-hex) is returned rather than
    raising, so a bad stored value can never make two things look identical.
    """
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return DHASH_SIDE * DHASH_SIDE  # maximally distant == never a match


# ---- blur / sharpness ------------------------------------------------------

def blur_variance(image: Image.Image, side: int = BLUR_SIDE) -> float:
    """Variance of the discrete Laplacian over a normalized grayscale image.

    Low == blurry, high == sharp. The image is resized to a fixed square first
    so the score is comparable across resolutions. Computed in pure Python on
    the 4-neighbour Laplacian (no clamping, so both edge polarities count).
    """
    small = image.convert("L").resize((side, side), Image.Resampling.BILINEAR)
    data = small.load()
    n = 0
    total = 0.0
    total_sq = 0.0
    for y in range(1, side - 1):
        for x in range(1, side - 1):
            lap = (
                data[x - 1, y] + data[x + 1, y]
                + data[x, y - 1] + data[x, y + 1]
                - 4 * data[x, y]
            )
            total += lap
            total_sq += lap * lap
            n += 1
    if n == 0:
        return 0.0
    mean = total / n
    return max(0.0, total_sq / n - mean * mean)


def sharpness_hint(blur: Optional[float], threshold: float = DEFAULT_BLUR_THRESHOLD) -> Optional[bool]:
    """True == likely blurry, False == looks sharp, None == unknown/not scored."""
    if blur is None:
        return None
    return blur < threshold


# ---- combined computation for a file --------------------------------------

def analyze(path: str | Path, kind: str = KIND_IMAGE) -> tuple[Optional[str], Optional[float]]:
    """Compute ``(phash, blur)`` for an image file, defensively.

    Returns ``(None, None)`` for non-images or anything Pillow cannot decode
    (a truncated file, an unsupported HEIC without a plugin, etc.) — a missing
    perceptual signal must never break a scan.
    """
    if kind != KIND_IMAGE:
        return None, None
    try:
        with Image.open(path) as im:
            im.load()
            return dhash(im), round(blur_variance(im), 2)
    except Exception:  # noqa: BLE001 — any decode failure -> no perceptual signal
        return None, None


# ---- near-duplicate clustering --------------------------------------------

def cluster_near_duplicates(
    items: Iterable[tuple[int, str]],
    max_distance: int = DEFAULT_NEAR_DUP_DISTANCE,
) -> list[list[int]]:
    """Group ids whose dhashes are within ``max_distance`` of each other.

    ``items`` is an iterable of ``(id, phash_hex)``. Returns clusters (each a
    list of ids, size >= 2) of visually-similar images. Byte-identical items
    (distance 0) are included — callers that only want *near* duplicates should
    exclude same-content ids beforehand. Uses union-find; O(n^2) pairwise, which
    is fine for personal-vault scales (tens of thousands of photos).
    """
    entries = [(i, h) for i, h in items if h]
    parent = {i: i for i, _ in entries}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    n = len(entries)
    for a in range(n):
        ia, ha = entries[a]
        for b in range(a + 1, n):
            ib, hb = entries[b]
            if hamming(ha, hb) <= max_distance:
                union(ia, ib)

    groups: dict[int, list[int]] = {}
    for i, _ in entries:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values() if len(g) > 1]
