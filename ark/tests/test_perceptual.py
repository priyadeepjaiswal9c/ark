"""Perceptual primitives: dHash, blur variance, Hamming, near-dup clustering."""

from PIL import Image, ImageDraw, ImageFilter

from ark import perceptual as P


def _scene(seed: int, size=(256, 256)) -> Image.Image:
    """A deterministic, visually-distinct little scene (no RNG so it's stable)."""
    img = Image.new("RGB", size, (20 + seed * 7 % 200, 40, 60))
    d = ImageDraw.Draw(img)
    for k in range(6):
        x = (seed * 31 + k * 47) % size[0]
        y = (seed * 17 + k * 29) % size[1]
        c = ((seed * 53 + k * 11) % 256, (seed * 29) % 256, (k * 40) % 256)
        d.rectangle([x, y, x + 60, y + 50], fill=c)
    return img


def test_dhash_is_stable_and_64_bit():
    im = _scene(1)
    h1, h2 = P.dhash(im), P.dhash(im.copy())
    assert h1 == h2                 # deterministic
    assert len(h1) == 16            # 64-bit -> 16 hex chars
    assert P.hamming(h1, h2) == 0


def test_dhash_survives_recompress_and_resize():
    im = _scene(2)
    # a downscale + upscale round-trip (like a re-saved edit) stays "the same"
    edit = im.resize((128, 128)).resize((256, 256))
    assert P.hamming(P.dhash(im), P.dhash(edit)) <= P.DEFAULT_NEAR_DUP_DISTANCE


def test_distinct_scenes_are_far_apart():
    hashes = [P.dhash(_scene(s)) for s in range(1, 8)]
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            assert P.hamming(hashes[i], hashes[j]) > P.DEFAULT_NEAR_DUP_DISTANCE


def test_hamming_tolerates_bad_input():
    # a corrupt/None stored hash must read as maximally distant, never as a match
    assert P.hamming("not-hex", "0" * 16) == 64
    assert P.hamming(None, "0" * 16) == 64


def test_blur_variance_separates_sharp_from_blurry():
    sharp = _scene(3)
    blurry = sharp.filter(ImageFilter.GaussianBlur(6))
    vs, vb = P.blur_variance(sharp), P.blur_variance(blurry)
    assert vs > vb * 5              # sharp has far more edge energy
    assert P.sharpness_hint(vb, threshold=vs) is True
    assert P.sharpness_hint(vs, threshold=vb) is False
    assert P.sharpness_hint(None) is None


def test_cluster_groups_near_excludes_far():
    a = P.dhash(_scene(4))
    a_edit = P.dhash(_scene(4).resize((150, 150)).resize((256, 256)))
    far = P.dhash(_scene(5))
    clusters = P.cluster_near_duplicates([(1, a), (2, a_edit), (3, far)])
    assert clusters == [[1, 2]]     # the near pair groups; the far one is alone


def test_analyze_skips_non_images(tmp_path):
    doc = tmp_path / "note.txt"
    doc.write_text("hello")
    assert P.analyze(doc, kind="document") == (None, None)
    # an undecodable "image" fails soft, not loud
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff not-a-real-jpeg")
    assert P.analyze(bad, kind="image") == (None, None)
