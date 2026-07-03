from pathlib import Path

from ark.hashing import hash_file, hash_bytes, verify_copy
from ark import geocode


def test_hash_is_deterministic_and_streaming(tmp_path):
    p = tmp_path / "f.bin"
    data = b"ark" * 100000
    p.write_bytes(data)
    assert hash_file(p) == hash_bytes(data)


def test_verify_copy(tmp_path):
    src = tmp_path / "a"; src.write_bytes(b"hello world" * 10)
    dst = tmp_path / "b"; dst.write_bytes(b"hello world" * 10)
    ok, h = verify_copy(src, dst, expected=hash_file(src))
    assert ok and h == hash_file(src)
    dst.write_bytes(b"different")
    ok2, _ = verify_copy(src, dst, expected=hash_file(src))
    assert ok2 is False


def test_reverse_geocode_known_places():
    goa = geocode.reverse(15.51, 73.81)
    assert goa is not None and goa.admin == "Goa" and goa.country == "India"
    sf = geocode.reverse(37.77, -122.42)
    assert sf.city == "San Francisco"


def test_reverse_geocode_rejects_bad_coords():
    assert geocode.reverse(999, 999) is None
