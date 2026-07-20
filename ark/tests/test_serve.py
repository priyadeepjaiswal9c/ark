"""The sync receiver: non-destructive upload ingest + the HTTP surface (token
auth, /ingest, /status, static shell)."""

import io
import json
import socket
import threading
import argparse
from pathlib import Path
from types import SimpleNamespace

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


def test_truncated_http_upload_is_rejected_and_staging_is_retained(vault, monkeypatch):
    declared = 20
    partial = b"only-partial"
    handler = object.__new__(S._Handler)
    handler.path = "/ingest"
    handler.headers = {
        "Authorization": "Bearer secret-tok",
        "Content-Length": str(declared),
        "X-Filename": "partial.bin",
    }
    handler.rfile = io.BytesIO(partial)
    handler.connection = SimpleNamespace(settimeout=lambda timeout: None)
    released = []
    handler.server = SimpleNamespace(
        token="secret-tok", vault=vault, cfg=default_config(vault),
        reserve_upload=lambda length, inbox: True,
        release_upload=lambda length: released.append(length),
    )
    response = {}
    handler._send_json = lambda code, payload: response.update(code=code, payload=payload)
    monkeypatch.setattr(
        S, "ingest_staged",
        lambda *args, **kwargs: pytest.fail("truncated bytes must not reach the pipeline"),
    )

    handler.do_POST()

    assert response["code"] == 400
    assert response["payload"]["ok"] is False
    assert "truncated upload" in response["payload"]["error"]
    assert released == [declared]
    staged = list((vault / DIR_META / "inbox").iterdir())
    assert len(staged) == 1 and staged[0].read_bytes() == partial


def test_pipeline_exception_retains_staged_upload(vault, monkeypatch):
    data = b"the sender's only delivered copy"
    received = S.stage_upload_stream(vault, "keep.bin", io.BytesIO(data), len(data))

    def fail_mid_pipeline(*args, **kwargs):
        raise RuntimeError("simulated mid-pipeline failure")

    monkeypatch.setattr(S.pipeline, "run", fail_mid_pipeline)
    out = S.ingest_staged(vault, default_config(vault), received.filename, received.path)

    assert out.ok is False and "mid-pipeline failure" in out.error
    assert received.path.is_file() and received.path.read_bytes() == data


def test_upload_quota_and_connection_caps_reject_excess_load(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "already-staged").write_bytes(b"12345")
    monkeypatch.setattr(S, "_MAX_OUTSTANDING", 12)

    server = object.__new__(S.ArkServer)
    server._quota_lock = threading.Lock()
    server._reserved_bytes = 0
    assert server.reserve_upload(7, inbox) is True       # 5 staged + 7 in flight
    assert server.reserve_upload(1, inbox) is False      # would exceed the cap
    server.release_upload(7)

    server._connection_slots = threading.BoundedSemaphore(S._MAX_CONNECTIONS)
    for _ in range(S._MAX_CONNECTIONS):
        assert server._connection_slots.acquire(blocking=False)
    sent = []
    closed = []
    request = SimpleNamespace(sendall=lambda data: sent.append(data))
    server.shutdown_request = lambda req: closed.append(req)

    S.ArkServer.process_request(server, request, ("test-client", 1234))

    assert S.ArkServer.request_queue_size == S._MAX_CONNECTIONS
    assert sent and b"503 Service Unavailable" in sent[0]
    assert closed == [request]


def test_bearer_authorization_header_is_primary_and_query_token_is_rejected():
    handler = object.__new__(S._Handler)
    handler.server = SimpleNamespace(token="secret-tok")
    handler.path = "/status"
    handler.headers = {"Authorization": "Bearer secret-tok"}
    assert handler._authed() is True

    handler.path = "/status?t=secret-tok"
    handler.headers = {}
    assert handler._authed() is False


# ---- HTTP surface ----------------------------------------------------------

@pytest.fixture
def server(vault):
    cfg = default_config(vault)
    # Build the real ArkServer state without binding a listening TCP port. The
    # execution sandbox forbids bind(2), so requests below traverse the real
    # BaseHTTPRequestHandler over a local socket pair instead.
    srv = object.__new__(S.ArkServer)
    srv.vault = vault
    srv.cfg = cfg
    srv.token = "secret-tok"
    srv._quota_lock = threading.Lock()
    srv._reserved_bytes = 0
    yield srv, srv


def _req(server, method, path, body=None, headers=None):
    body = body or b""
    request_headers = {"Host": "localhost", "Connection": "close", **(headers or {})}
    if body:
        request_headers.setdefault("Content-Length", str(len(body)))
    head = "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
    raw = f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode("ascii") + body

    client, handler_socket = socket.socketpair()
    try:
        client.settimeout(5)
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        S._Handler(handler_socket, ("local", 0), server)
        handler_socket.close()
        response = b""
        while chunk := client.recv(1 << 16):
            response += chunk
    finally:
        client.close()
        handler_socket.close()
    status_line, remainder = response.split(b"\r\n", 1)
    _, status, _ = status_line.split(b" ", 2)
    _, payload = remainder.split(b"\r\n\r\n", 1)
    return int(status), payload


def test_http_ingest_roundtrip(server, vault):
    _, transport = server
    status, data = _req(transport, "POST", "/ingest", body=_png_bytes((9, 9, 9)),
                        headers={"X-ARK-Token": "secret-tok", "X-Filename": "phone.png"})
    assert status == 200
    body = json.loads(data)
    assert body["ok"] and body["status"] == "stored"
    with Database(vault / DIR_META / DB_FILENAME) as db:
        assert db.stats()["assets"] == 1


def test_http_requires_token(server):
    _, transport = server
    status, _ = _req(transport, "POST", "/ingest", body=b"x",
                     headers={"X-Filename": "x.png"})               # no token
    assert status == 401
    status2, _ = _req(transport, "GET", "/status", headers={"X-ARK-Token": "wrong"})
    assert status2 == 401


def test_http_status_and_shell(server):
    _, transport = server
    status, data = _req(transport, "GET", "/status", headers={"X-ARK-Token": "secret-tok"})
    assert status == 200 and json.loads(data)["ok"] is True
    # the app shell is served and self-identifies
    st, html = _req(transport, "GET", "/")
    assert st == 200 and b"Send to" in html and b"/manifest.webmanifest" in html
    # manifest + service worker are reachable (installable PWA)
    assert _req(transport, "GET", "/manifest.webmanifest")[0] == 200
    assert _req(transport, "GET", "/sw.js")[0] == 200


def test_http_static_no_traversal(server):
    _, transport = server
    st, _ = _req(transport, "GET", "/../ark.db")
    assert st in (403, 404)              # never escape the webapp dir
