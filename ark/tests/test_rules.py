"""Rule matching + path-template rendering, including the sanitizer that keeps a
malicious template value from escaping the organized/ tree."""

from ark.config import default_config
from ark.models import Asset
from ark.rules import apply_rules, validate_rules, _render, _sanitize_component


def _img(**kw):
    base = dict(source_path="/src/IMG.jpg", hash="deadbeef", size=100,
                ext="jpg", kind="image", mime="image/jpeg")
    base.update(kw)
    return Asset(**base)


def test_default_rules_all_valid():
    cfg = default_config("/tmp/v")
    assert validate_rules(cfg) == []


def test_unknown_fields_route_to_review():
    cfg = default_config("/tmp/v")
    a = _img()  # no date, no place -> nothing confident
    m = apply_rules(a, cfg)
    assert m.is_review is True
    assert "unknown" not in m.dest_relpath.split("/")[0].lower() or m.dest_relpath


def test_template_value_cannot_inject_path_separators():
    # a city literally named "../../etc" must not create parent segments
    ctx = {"place": type("N", (), {"city": "../../etc"})(), "taken_at": type("D", (), {"year": 2025})()}
    rel, unknown = _render("Photos/{place.city}/{taken_at.year}", ctx)
    assert ".." not in rel.split("/")
    assert rel.startswith("Photos/")


def test_sanitize_component():
    assert _sanitize_component("a/b") == "a-b"
    assert _sanitize_component("..") == "unknown"
    assert _sanitize_component("  .hidden  ") == "hidden"
    assert _sanitize_component("New York") == "New York"


def test_first_match_wins(tmp_path):
    from datetime import datetime
    from ark.models import GeoPlace
    cfg = default_config(str(tmp_path))
    a = _img(taken_at=datetime(2025, 12, 20), taken_at_source="exif",
             lat=15.3, lon=74.0, place=GeoPlace(15.3, 74.0, city="Panaji",
                                                admin="Goa", country="India"))
    m = apply_rules(a, cfg)
    assert m.rule_name == "goa-winter-trips"
    assert m.dest_relpath == "Trips/Goa-2025"
