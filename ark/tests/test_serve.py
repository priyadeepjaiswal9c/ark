"""The sync receiver: non-destructive upload ingest + the HTTP surface (token
auth, /ingest, /status, static shell)."""

import http.client
import io
import json
import threading
import argparse
from pathlib import Path

import pytest
from PIL import Image

from ark import serve as S
from ark.config import default_config
from ark.cli import cmd_init
from ark.constants import DIR_META, DB_FILENAME
from ark.db import Database


def _png_bytes(color=(120, 60, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "png")
    return buf.getvalue()


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    cmd_init(argparse.Namespace(vault=str(v), backup=None, json=True))
    return v


def test_ingest_upload_backs_up_and_cleans_staging(vault):
    cfg = default_config(vault)
    out = S.ingest_upload(vault, cfg, "trip.png", _png_bytes())
    assert out.ok and out.status == "stored" and out.hash
    # the object is really in the vault, hash-addressed
    objs = list((vault / "objects").rglob("*"))
    assert any(o.is_file() and o.stem == out.hash for o in objs)
    # staging temp was removed after a verified backup (no inbox litter)
    inbox = vault / DIR_META / "inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


def test_ingest_upload_dedups_identical_bytes(vault):
    cfg = default_config(vault)
    data = _png_bytes((10, 20, 30))
    first = S.ingest_upload(vault, cfg, "a.png", data)
    second = S.ingest_upload(vault, cfg, "b.png", data)
    assert first.status == "stored" and second.status == "duplicate"
    assert first.hash == second.hash
    assert len([o for o in (vault / "objects").rglob("*") if o.is_file()]) == 1


def test_bad_filename_is_sanitised(vault):
    cfg = default_config(vault)
    out = S.ingest_upload(vault, cfg, "../../etc/passwd", _png_bytes((1, 2, 3)))
    assert out.ok and "/" not in out.filename and ".." not in out.filename


# ---- HTTP surface ----------------------------------------------------------

@pytest.fixture
def server(vault):
    cfg = default_config(vault)
    srv = S.make_server(vault, cfg, "127.0.0.1", 0, token="secret-tok")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_http_ingest_roundtrip(server, vault):
    _, port = server
    status, data = _req(port, "POST", "/ingest", body=_png_bytes((9, 9, 9)),
                        headers={"X-ARK-Token": "secret-tok", "X-Filename": "phone.png"})
    assert status == 200
    body = json.loads(data)
    assert body["ok"] and body["status"] == "stored"
    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert db.stats()["assets"] == 1


def test_http_requires_token(server):
    _, port = server
    status, _ = _req(port, "POST", "/ingest", body=b"x",
                     headers={"X-Filename": "x.png"})               # no token
    assert status == 401
    status2, _ = _req(port, "GET", "/status", headers={"X-ARK-Token": "wrong"})
    assert status2 == 401


def test_http_status_and_shell(server):
    _, port = server
    status, data = _req(port, "GET", "/status", headers={"X-ARK-Token": "secret-tok"})
    assert status == 200 and json.loads(data)["ok"] is True
    # the app shell is served and self-identifies
    st, html = _req(port, "GET", "/")
    assert st == 200 and b"Send to" in html and b"/manifest.webmanifest" in html
    # manifest + service worker are reachable (installable PWA)
    assert _req(port, "GET", "/manifest.webmanifest")[0] == 200
    assert _req(port, "GET", "/sw.js")[0] == 200


def test_http_static_no_traversal(server):
    _, port = server
    st, _ = _req(port, "GET", "/../ark.db")
    assert st in (403, 404)              # never escape the webapp dir
